"""Broker Reconciliation — match `broker_orders` against MC's internal records.

Doctrine pin (2026-05-23):
    The orphan audit surfaced ~500 fills in `broker_orders` with
    `source=access_key` and no matching `execution_receipts`. The
    operator's 6-step plan calls for explicit reconciliation that:
      1. iterates every `broker_orders` row
      2. searches for a matching MC `execution_receipts` (by
         broker_order_id) — that's the doctrinal seal
      3. (best-effort) searches for a matching `shared_intents`
         (by symbol + side within ±90 min of submitted_at) — that's
         a SOFT match, useful for forensics but never a substitute
         for the receipt
      4. tags every UNMATCHED row with
         `provenance="UNVERIFIED_BROKER_EXECUTION"` so the memory
         kernel permanently refuses to train on them
      5. writes one summary row per pass into `broker_reconciliation`

    Re-runnable: every row uses the broker_order_id as the upsert
    key. Running the reconcile a second time updates classifications
    if new receipts were written in the meantime (e.g., a slow MC
    write that landed after the first sweep).

Endpoints:
    POST /api/admin/broker/reconcile           — run a reconcile pass
    GET  /api/admin/broker/reconcile/summary   — current state
    GET  /api/admin/broker/reconcile/unverified — list unverified orders
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from auth import get_current_user
from db import db
from namespaces import (
    BROKER_RECONCILIATION,
    EXECUTION_RECEIPTS,
    SHARED_INTENTS,
)


logger = logging.getLogger(__name__)


router = APIRouter(prefix="/admin/broker", tags=["broker-reconcile"])


# Doctrinal label propagated into `broker_orders.provenance` and
# `memory_kernel_quarantine.provenance_explicit` whenever a row lacks
# an MC execution receipt.
UNVERIFIED = "UNVERIFIED_BROKER_EXECUTION"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        # Alpaca timestamps look like "2026-05-15T13:23:02.058599Z"
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


# ─────────────────────────── core reconciler ───────────────────────────


async def _find_matching_receipt(
    broker_order_id: str, symbol: str,
) -> Optional[Dict[str, Any]]:
    """Look up an MC execution receipt by broker_order_id. That's the
    only doctrinally valid match. We do NOT fall back to fuzzy matching
    here — fuzzy matches go into `shared_intent_hint`."""
    return await db[EXECUTION_RECEIPTS].find_one(
        {"broker_order_id": broker_order_id},
        {"_id": 0, "receipt_id": 1, "intent_id": 1, "broker_order_id": 1, "ts": 1},
    )


async def _find_intent_hint(
    symbol: str, side: Optional[str], submitted_at: Optional[datetime],
) -> Optional[Dict[str, Any]]:
    """Soft, forensics-only hint match against `shared_intents`. NOT a
    substitute for an execution_receipts match — used only to tell the
    operator "here's the intent that LOOKED like this fill, if any"."""
    if not symbol or not side or not submitted_at:
        return None
    lo = (submitted_at - timedelta(minutes=90)).isoformat()
    hi = (submitted_at + timedelta(minutes=90)).isoformat()
    side_norm = side.upper()
    action = "BUY" if side_norm == "BUY" else "SELL"
    return await db[SHARED_INTENTS].find_one(
        {
            "symbol": symbol,
            "action": action,
            "ingest_ts": {"$gte": lo, "$lte": hi},
        },
        {"_id": 0, "intent_id": 1, "stack": 1, "ingest_ts": 1,
         "gate_state": 1, "executed": 1},
        sort=[("ingest_ts", 1)],
    )


async def _reconcile_one(order: Dict[str, Any]) -> Dict[str, Any]:
    """Reconcile a single broker_orders row. Returns the classification
    dict that will be merged into the row + the reconciliation log."""
    broker_order_id = order.get("broker_order_id")
    symbol = order.get("symbol")
    side = order.get("side")
    submitted_at = _parse_iso(order.get("submitted_at"))

    receipt = await _find_matching_receipt(broker_order_id, symbol) if broker_order_id else None

    if receipt:
        classification = {
            "provenance": "VERIFIED_MC_EXECUTION",
            "mc_receipt_id": receipt.get("receipt_id"),
            "mc_intent_id": receipt.get("intent_id"),
            "verified": True,
        }
    else:
        hint = await _find_intent_hint(symbol, side, submitted_at)
        classification = {
            "provenance": UNVERIFIED,
            "verified": False,
            "intent_hint": hint,
            "reason": (
                "no_execution_receipt"
                if broker_order_id
                else "no_broker_order_id"
            ),
        }
    classification["reconciled_at"] = _now_iso()
    return classification


async def _persist_reconciliation(
    broker_order_id: str, classification: Dict[str, Any],
) -> None:
    """Update the broker_orders row AND upsert the reconciliation log."""
    await db.broker_orders.update_one(
        {"broker_order_id": broker_order_id},
        {"$set": {
            "provenance": classification["provenance"],
            "verified": classification["verified"],
            "reconciled_at": classification["reconciled_at"],
            "mc_receipt_id": classification.get("mc_receipt_id"),
            "mc_intent_id": classification.get("mc_intent_id"),
        }},
    )
    # Quarantine row: propagate the explicit label so the memory kernel
    # surface uses the doctrinally-correct provenance string.
    if not classification["verified"]:
        await db.memory_kernel_quarantine.update_many(
            {"broker_order_id": broker_order_id},
            {"$set": {
                "provenance_explicit": UNVERIFIED,
                "reconciled_at": classification["reconciled_at"],
            }},
        )
    # Append-only reconciliation log row.
    await db[BROKER_RECONCILIATION].update_one(
        {"broker_order_id": broker_order_id},
        {"$set": {
            "broker_order_id": broker_order_id,
            **classification,
        }},
        upsert=True,
    )


# ─────────────────────────── routes ───────────────────────────


class ReconcileIn(BaseModel):
    after: Optional[str] = Field(default=None, description="ISO-8601 lower bound on filled_at")
    until: Optional[str] = Field(default=None, description="ISO-8601 upper bound on filled_at")
    limit: int = Field(default=10000, ge=1, le=50000)


@router.post("/reconcile")
async def run_reconcile(
    body: ReconcileIn,
    user: dict = Depends(get_current_user),  # noqa: B008
):
    """Run a reconciliation pass over `broker_orders`.

    Idempotent. Safe to re-run — every classification upserts by
    broker_order_id.
    """
    q: Dict[str, Any] = {}
    if body.after or body.until:
        rng: Dict[str, Any] = {}
        if body.after:
            rng["$gte"] = body.after
        if body.until:
            rng["$lte"] = body.until
        q["filled_at"] = rng

    cursor = db.broker_orders.find(q, {"_id": 0}).limit(body.limit)
    counts = {
        "VERIFIED_MC_EXECUTION": 0,
        UNVERIFIED: 0,
        "errors": 0,
        "total": 0,
    }
    async for order in cursor:
        counts["total"] += 1
        bid = order.get("broker_order_id")
        if not bid:
            counts["errors"] += 1
            continue
        try:
            classification = await _reconcile_one(order)
            await _persist_reconciliation(bid, classification)
            counts[classification["provenance"]] = counts.get(
                classification["provenance"], 0) + 1
        except Exception as e:  # noqa: BLE001
            logger.warning("reconcile failed for %s: %r", bid, e)
            counts["errors"] += 1

    actor = user.get("email") or "operator"
    logger.info(
        "broker reconcile by %s: window=%s..%s counts=%s",
        actor, body.after, body.until, counts,
    )
    return {
        "ok": True,
        "actor": actor,
        "window": {"after": body.after, "until": body.until},
        "counts": counts,
        "doctrine_note": (
            "VERIFIED_MC_EXECUTION = MC issued the order (receipt found). "
            f"{UNVERIFIED} = no MC receipt; broker fired without MC. "
            "Unverified orders are tagged in broker_orders + "
            "memory_kernel_quarantine. Never trainable by doctrine."
        ),
    }


@router.get("/reconcile/summary")
async def reconcile_summary(_user: dict = Depends(get_current_user)):  # noqa: B008
    by_prov: List[Dict[str, Any]] = await db.broker_orders.aggregate([
        {"$group": {"_id": "$provenance", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
    ]).to_list(50)
    total = await db.broker_orders.count_documents({})
    unreconciled = await db.broker_orders.count_documents({"provenance": {"$exists": False}})
    return {
        "total_broker_orders": total,
        "unreconciled": unreconciled,
        "by_provenance": [
            {"provenance": r["_id"] or "(unreconciled)", "count": r["count"]}
            for r in by_prov
        ],
    }


@router.get("/reconcile/unverified")
async def list_unverified(
    limit: int = 100,
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    """Operator-readable list of orders the broker fired that MC never
    issued. These are forever non-trainable."""
    items: List[Dict[str, Any]] = await db.broker_orders.find(
        {"provenance": UNVERIFIED},
        {"_id": 0},
    ).sort("filled_at", -1).limit(min(max(limit, 1), 500)).to_list(min(max(limit, 1), 500))
    return {
        "label": UNVERIFIED,
        "count": len(items),
        "items": items,
    }
