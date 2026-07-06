"""Kraken manual reconcile — operator-triggered, single-intent version.

Doctrine (2026-07-06, operator sign-off "Option C"):
    * Crypto goes live Monday. Without a Kraken reconciliation
      path, every successful Kraken submit becomes a stuck
      `gate_state='submitted'` intent — same P1 bug the equity
      lane just paid to fix, but for the crypto lane.
    * We WILL NOT auto-sweep Kraken orders today. The auto_router
      reconcile sweep stays equity-only because we have no way
      to smoke-test against a live Kraken account on preview
      (unfunded, no creds available for the sweep to burn on
      dry-runs). Shipping an auto-firing background job that has
      never seen a real Kraken order response is the exact
      "green test hiding an ungenerated signal" failure mode
      we spent this session refusing to accept.
    * INSTEAD: operator-triggered manual reconciliation. When a
      Monday crypto intent is stuck at `submitted`, the operator
      hits this endpoint with the intent_id. The Kraken adapter's
      normalized response is applied to the intent using the SAME
      transition logic the auto_router sweep uses for equity.
      First live Kraken response is a deliberate operator action,
      not a background firehose.

    * Follow-up (after Monday's data): promote to auto-sweep by
      extending `_sweep_submitted_broker_orders` to iterate both
      lanes. That work is estimated at ~15 LOC + 4 tests.

Route surface:
    POST /api/admin/kraken-reconcile/reconcile-intent
    Body: {"intent_id": "..."} or {"txid": "..."}

Returns a diagnostic dict describing what the endpoint OBSERVED and
what it DID:
    {
        "found_intent": bool,
        "intent_id": str | None,
        "kraken_txid": str | None,
        "kraken_status": str | None,
        "kraken_response_shape": {...},
        "action_taken": "filled" | "rejected_terminal"
                        | "rejected_retry" | "no_change"
                        | "adapter_error" | "no_credentials"
                        | "not_found",
        "new_gate_state": str | None,
        "detail": str,
    }
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth import get_current_user
from db import db
from namespaces import SHARED_INTENTS


router = APIRouter(prefix="/admin/kraken-reconcile", tags=["kraken-reconcile"])


class ReconcileIn(BaseModel):
    intent_id: Optional[str] = None
    txid: Optional[str] = None


# Kept in sync with `auto_router.RECONCILE_MAX_RETRIES` — this
# endpoint uses IDENTICAL transition logic to the auto_router
# reconcile sweep so manual reconciliation produces the same
# state-machine outcome as a future auto-sweep would.
_MAX_RETRIES = 3


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@router.post("/reconcile-intent")
async def reconcile_intent(
    body: ReconcileIn,
    _user: dict = Depends(get_current_user),
):
    """Manually reconcile a single crypto intent against Kraken's
    live order state. Operator-triggered.

    Exactly one of `intent_id` or `txid` must be provided. If
    `intent_id` is given, we look up the intent and use its stored
    `broker_order.id` as the Kraken txid. If `txid` is given
    directly, we look up the intent by matching `broker_order.id`.
    """
    if not (body.intent_id or body.txid):
        raise HTTPException(400, "must supply intent_id or txid")

    # ── 1. Load intent ────────────────────────────────────────────
    q = ({"intent_id": body.intent_id} if body.intent_id
         else {"broker_order.id": body.txid})
    intent = await db[SHARED_INTENTS].find_one(
        q,
        {"_id": 0, "intent_id": 1, "lane": 1, "gate_state": 1,
         "broker_order": 1, "submit_retry_count": 1, "symbol": 1},
    )
    if not intent:
        return {
            "found_intent": False,
            "intent_id": body.intent_id,
            "kraken_txid": body.txid,
            "action_taken": "not_found",
            "detail": "no shared_intents doc matches the given key",
        }

    intent_id = intent["intent_id"]
    lane = (intent.get("lane") or "").lower()
    if lane != "crypto":
        raise HTTPException(
            400,
            f"intent {intent_id} is lane={lane!r}; this endpoint "
            "handles crypto only. Use the equity auto-sweep for "
            "equity intents.",
        )

    bo_meta = intent.get("broker_order") or {}
    txid = bo_meta.get("id") or bo_meta.get("order_id")
    if not txid:
        return {
            "found_intent": True,
            "intent_id": intent_id,
            "kraken_txid": None,
            "action_taken": "not_found",
            "detail": "intent has no broker_order.id — was it ever submitted?",
        }

    # ── 2. Fetch Kraken creds ─────────────────────────────────────
    from shared.crypto.kraken import get_active_keys, query_order, KrakenError
    from shared.broker_error_taxonomy import classify

    keys = await get_active_keys()
    if keys is None:
        return {
            "found_intent": True,
            "intent_id": intent_id,
            "kraken_txid": txid,
            "action_taken": "no_credentials",
            "detail": (
                "Kraken singleton credential is missing/undecryptable. "
                "See /api/admin/kraken/status for details."
            ),
        }
    public_key, private_key = keys

    # ── 3. Query Kraken ───────────────────────────────────────────
    try:
        bo = await query_order(str(txid), public_key, private_key)
    except KrakenError as exc:
        return {
            "found_intent": True,
            "intent_id": intent_id,
            "kraken_txid": txid,
            "action_taken": "adapter_error",
            "detail": f"Kraken API error: {exc}",
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "found_intent": True,
            "intent_id": intent_id,
            "kraken_txid": txid,
            "action_taken": "adapter_error",
            "detail": f"transport/other: {exc}",
        }

    status = (bo.get("status") or "").upper()

    # ── 4. Apply transition (mirrors auto_router sweep logic) ─────
    if status == "FILLED":
        await db[SHARED_INTENTS].update_one(
            {"intent_id": intent_id},
            {"$set": {
                "gate_state": "filled",
                "filled_at": _now_iso(),
                "reconciled_by": _user.get("email", "operator"),
                "reconciled_manually": True,
                "broker_order.status": "FILLED",
                "broker_order.filled_qty": bo.get("filled_qty"),
                "broker_order.filled_avg_price": bo.get("filled_avg_price"),
                "broker_order.filled_at": bo.get("filled_at"),
            }},
        )
        return {
            "found_intent": True,
            "intent_id": intent_id,
            "kraken_txid": txid,
            "kraken_status": status,
            "kraken_response_shape": bo,
            "action_taken": "filled",
            "new_gate_state": "filled",
            "detail": (
                f"filled_qty={bo.get('filled_qty')} "
                f"avg_price={bo.get('filled_avg_price')}"
            ),
        }

    if status in {"CANCELED", "CANCELLED", "REJECTED", "EXPIRED"}:
        reject_reason = str(
            bo.get("reject_reason")
            or bo.get("status_detail")
            or status
        )
        err = classify(reject_reason)
        retry_count = int(intent.get("submit_retry_count") or 0)

        if err.is_terminal or retry_count >= _MAX_RETRIES:
            await db[SHARED_INTENTS].update_one(
                {"intent_id": intent_id},
                {"$set": {
                    "gate_state": "broker_rejected",
                    "rejected_at": _now_iso(),
                    "reconciled_by": _user.get("email", "operator"),
                    "reconciled_manually": True,
                    "broker_reason": err.bucket,
                    "broker_error_detail": err.detail,
                    "broker_error_terminal": bool(err.is_terminal),
                    "submit_retry_count": retry_count,
                }},
            )
            return {
                "found_intent": True,
                "intent_id": intent_id,
                "kraken_txid": txid,
                "kraken_status": status,
                "kraken_response_shape": bo,
                "action_taken": "rejected_terminal",
                "new_gate_state": "broker_rejected",
                "detail": (
                    f"bucket={err.bucket} terminal={err.is_terminal} "
                    f"retries={retry_count}/{_MAX_RETRIES}"
                ),
            }
        # Transient under cap → requeue as pending.
        await db[SHARED_INTENTS].update_one(
            {"intent_id": intent_id},
            {
                "$set": {
                    "gate_state": "pending",
                    "executed": False,
                    "submit_retry_count": retry_count + 1,
                    "last_reject_at": _now_iso(),
                    "last_reject_bucket": err.bucket,
                    "last_reject_detail": err.detail,
                    "reconciled_by": _user.get("email", "operator"),
                    "reconciled_manually": True,
                },
                "$unset": {
                    "broker_order": "",
                    "executed_at": "",
                    "executed_by": "",
                },
            },
        )
        return {
            "found_intent": True,
            "intent_id": intent_id,
            "kraken_txid": txid,
            "kraken_status": status,
            "kraken_response_shape": bo,
            "action_taken": "rejected_retry",
            "new_gate_state": "pending",
            "detail": (
                f"bucket={err.bucket} retry={retry_count + 1}/{_MAX_RETRIES} "
                "— intent re-enters routing on next tick"
            ),
        }

    # OPEN / PENDING / WORKING → no-op, keep polling.
    return {
        "found_intent": True,
        "intent_id": intent_id,
        "kraken_txid": txid,
        "kraken_status": status,
        "kraken_response_shape": bo,
        "action_taken": "no_change",
        "new_gate_state": intent.get("gate_state"),
        "detail": (
            "Kraken says the order is still open/working. Poll again "
            "after the exchange decides."
        ),
    }
