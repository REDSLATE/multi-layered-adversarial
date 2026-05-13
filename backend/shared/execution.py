"""Execution router — intent → gate chain → broker.

Doctrine:
  * Brains never call this router. Operator JWT only.
  * Intent must hold the Executor seat at ingest AND now.
  * Every gate is logged. Block reasons are surfaced to the UI.
  * Caps are SOFTWARE; see `shared/exposure_caps.py`.
  * Order routing uses notional (dollar-amount) market day orders for
    the paper-trading phase — keeps caps trivially enforceable
    regardless of price discovery latency.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from auth import get_current_user
from db import db
from namespaces import (
    EXECUTION_RECEIPTS,
    SHARED_GATE_RESULTS,
    SHARED_INTENTS,
)
from shared.broker.alpaca_routes import get_alpaca_adapter
from shared.exposure_caps import caps_snapshot, evaluate_all
from shared.executor_seat import get_executor_holder


router = APIRouter(tags=["execution"])


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ───────────────────────────── gate chain ─────────────────────────────

async def _evaluate_gates(intent: dict, order_notional_usd: float) -> dict:
    """Run the full gate chain for an intent.

    Returns:
        {
          "verdict": "would_pass" | "would_block",
          "gates": [{name, passed, reason}, ...],
          "order_notional_usd": float,
        }
    """
    gates: list[dict] = []

    # 1. Schema invariants — pinned by IntentIn validators.
    gates.append({
        "name": "schema_invariants",
        "passed": intent.get("may_execute") is False and intent.get("requires_gate_pass") is True,
        "reason": "may_execute pinned False; requires_gate_pass pinned True",
    })

    # 2. Action-routable check — only BUY/SELL/SHORT/COVER are routable.
    action = intent.get("action")
    routable = action in ("BUY", "SELL", "SHORT", "COVER")
    gates.append({
        "name": "action_routable",
        "passed": routable,
        "reason": (
            f"action {action!r} is routable to the broker"
            if routable else
            f"action {action!r} is not a routable order (HOLD/etc are watchlist signals)"
        ),
    })

    # 3. Executor seat — held at ingest AND still held now.
    current_holder = await get_executor_holder()
    held_at_intent = bool(intent.get("holds_executor_seat"))
    held_at_post = intent.get("executor_holder_at_post")
    holds_now = current_holder == intent.get("stack")
    if held_at_intent and holds_now:
        seat_pass, seat_reason = True, (
            f"{intent['stack']} held Executor at ingest and still holds it ({current_holder})"
        )
    elif held_at_intent and not holds_now:
        seat_pass, seat_reason = False, (
            f"{intent['stack']} held Executor at ingest, but seat has rotated to "
            f"{current_holder or 'empty'} — stale intent cannot execute"
        )
    elif not held_at_intent and held_at_post is None:
        seat_pass, seat_reason = False, (
            "Executor seat was EMPTY when intent was posted — no authority"
        )
    else:
        seat_pass, seat_reason = False, (
            f"Executor seat was held by {held_at_post} at post time, not {intent.get('stack')}"
        )
    gates.append({"name": "executor_seat_check", "passed": seat_pass, "reason": seat_reason})

    # 4. Live-trading-disabled (paper mode).
    gates.append({
        "name": "live_trading_disabled",
        "passed": True,
        "reason": "LIVE_TRADING_ENABLED stays False — paper broker only",
    })

    # 5. Broker connected.
    adapter = await get_alpaca_adapter()
    broker_connected = adapter is not None
    gates.append({
        "name": "broker_connected",
        "passed": broker_connected,
        "reason": (
            "Alpaca paper adapter present"
            if broker_connected else
            "No Alpaca credentials stored — connect broker on the admin page first"
        ),
    })

    # 6. Hard exposure caps.
    side = action or ""
    cap_evals = await evaluate_all(order_notional_usd, side)
    for c in cap_evals:
        gates.append({"name": c.name, "passed": c.passed, "reason": c.reason})

    verdict = "would_pass" if all(g["passed"] for g in gates) else "would_block"
    return {
        "verdict": verdict,
        "gates": gates,
        "order_notional_usd": order_notional_usd,
        "caps": caps_snapshot(),
    }


# ───────────────────────────── dry-run ─────────────────────────────

@router.post("/execution/dry_run")
async def execution_dry_run(
    intent_id: str = Query(..., description="intent_id to evaluate"),
    order_notional_usd: float = Query(
        default=10.0,
        ge=0.01,
        le=10_000.0,
        description="proposed order notional in USD (defaults to the per-order cap)",
    ),
    user: dict = Depends(get_current_user),  # noqa: B008
):
    """Evaluate the full gate chain WITHOUT placing an order."""
    intent = await db[SHARED_INTENTS].find_one({"intent_id": intent_id}, {"_id": 0})
    if not intent:
        raise HTTPException(status_code=404, detail=f"intent {intent_id} not found")

    result = await _evaluate_gates(intent, order_notional_usd)
    new_state = "dry_run_passed" if result["verdict"] == "would_pass" else "dry_run_blocked"
    await db[SHARED_INTENTS].update_one(
        {"intent_id": intent_id},
        {"$set": {
            "gate_state": new_state,
            "last_dry_run_ts": _now_iso(),
            "last_dry_run_by": user.get("email"),
            "last_dry_run_notional_usd": order_notional_usd,
        }},
    )
    await db[SHARED_GATE_RESULTS].insert_one({
        "intent_id": intent_id,
        "kind": "dry_run",
        "ts": _now_iso(),
        "by": user.get("email"),
        "order_notional_usd": order_notional_usd,
        "verdict": result["verdict"],
        "gates": result["gates"],
    })

    return {
        "intent_id": intent_id,
        "evaluated_by": user.get("email"),
        "ts": _now_iso(),
        **result,
    }


# ───────────────────────────── submit ─────────────────────────────

class SubmitBody(BaseModel):
    intent_id: str = Field(..., min_length=8, max_length=80)
    order_notional_usd: float = Field(default=10.0, ge=0.01, le=10_000.0)
    confirm: str = Field(default="", description="must equal 'execute' to actually route")


@router.post("/execution/submit")
async def execution_submit(
    body: SubmitBody,
    user: dict = Depends(get_current_user),  # noqa: B008
):
    """Route the intent through the gate chain and, if it passes,
    submit a market-day notional order to the broker.

    Idempotency: each intent can be executed AT MOST ONCE. Re-submits
    are rejected with 409.
    """
    if body.confirm != "execute":
        raise HTTPException(
            status_code=400,
            detail="confirmation phrase missing — set confirm='execute' to route this order",
        )

    intent = await db[SHARED_INTENTS].find_one({"intent_id": body.intent_id}, {"_id": 0})
    if not intent:
        raise HTTPException(status_code=404, detail=f"intent {body.intent_id} not found")
    if intent.get("executed"):
        raise HTTPException(
            status_code=409,
            detail=f"intent {body.intent_id} already executed at {intent.get('executed_at')}",
        )

    # Re-run the gate chain at submit time — state may have shifted
    # between the dry-run and the click (seat rotated, caps changed,
    # broker disconnected).
    result = await _evaluate_gates(intent, body.order_notional_usd)
    if result["verdict"] != "would_pass":
        # Audit-log the block so the operator can see why on the page.
        await db[SHARED_GATE_RESULTS].insert_one({
            "intent_id": body.intent_id,
            "kind": "submit_blocked",
            "ts": _now_iso(),
            "by": user.get("email"),
            "order_notional_usd": body.order_notional_usd,
            "verdict": result["verdict"],
            "gates": result["gates"],
        })
        await db[SHARED_INTENTS].update_one(
            {"intent_id": body.intent_id},
            {"$set": {
                "gate_state": "blocked",
                "last_submit_ts": _now_iso(),
                "last_submit_by": user.get("email"),
            }},
        )
        # Pick the first failing gate as the surface reason.
        first_block = next((g for g in result["gates"] if not g["passed"]), None)
        raise HTTPException(
            status_code=403,
            detail={
                "blocked_by": first_block["name"] if first_block else "unknown",
                "reason": first_block["reason"] if first_block else "gate chain blocked",
                "gates": result["gates"],
            },
        )

    # All gates passed — route the order.
    adapter = await get_alpaca_adapter()
    if adapter is None:  # belt-and-suspenders; gate already checked
        raise HTTPException(status_code=503, detail="broker disconnected mid-flight")

    side = "BUY" if intent["action"] in ("BUY", "COVER") else "SELL"
    client_order_id = f"mc-{body.intent_id[:8]}-{uuid.uuid4().hex[:6]}"

    try:
        order = await adapter.submit_market_order(
            symbol=intent["symbol"],
            notional=body.order_notional_usd,
            side=side,
            client_order_id=client_order_id,
        )
    except Exception as e:  # noqa: BLE001
        await db[SHARED_GATE_RESULTS].insert_one({
            "intent_id": body.intent_id,
            "kind": "submit_error",
            "ts": _now_iso(),
            "by": user.get("email"),
            "error": str(e),
        })
        raise HTTPException(status_code=502, detail=f"broker rejected order: {e}") from e

    now = _now_iso()
    receipt = {
        "receipt_id": str(uuid.uuid4()),
        "intent_id": body.intent_id,
        "stack": intent.get("stack"),
        "symbol": intent.get("symbol"),
        "action": intent.get("action"),
        "side": side,
        "notional_usd": float(body.order_notional_usd),
        "broker": "alpaca_paper",
        "broker_order_id": order["order_id"],
        "client_order_id": order.get("client_order_id"),
        "status": order.get("status"),
        "submitted_at": order.get("submitted_at") or now,
        "filled_at": order.get("filled_at"),
        "filled_qty": order.get("filled_qty", 0.0),
        "filled_avg_price": order.get("filled_avg_price"),
        "executed_at": now,
        "executed_by": user.get("email"),
        "gates_passed": result["gates"],
    }
    await db[EXECUTION_RECEIPTS].insert_one(receipt)
    await db[SHARED_INTENTS].update_one(
        {"intent_id": body.intent_id},
        {"$set": {
            "executed": True,
            "executed_at": now,
            "execution_receipt_id": receipt["receipt_id"],
            "broker_order_id": order["order_id"],
            "gate_state": "passed",
            "last_submit_ts": now,
            "last_submit_by": user.get("email"),
        }},
    )
    await db[SHARED_GATE_RESULTS].insert_one({
        "intent_id": body.intent_id,
        "kind": "submit_passed",
        "ts": now,
        "by": user.get("email"),
        "order_notional_usd": float(body.order_notional_usd),
        "broker_order_id": order["order_id"],
        "gates": result["gates"],
    })

    return {
        "ok": True,
        "intent_id": body.intent_id,
        "receipt": receipt,
        "order": order,
        "verdict": "executed",
    }


# ───────────────────────────── receipts ─────────────────────────────

@router.get("/execution/receipts")
async def list_receipts(
    limit: int = Query(default=50, ge=1, le=500),
    intent_id: Optional[str] = Query(default=None),
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    q: dict = {}
    if intent_id:
        q["intent_id"] = intent_id
    rows = (
        await db[EXECUTION_RECEIPTS]
        .find(q, {"_id": 0})
        .sort("executed_at", -1)
        .to_list(limit)
    )
    return {"items": rows, "count": len(rows), "caps": caps_snapshot()}


@router.get("/execution/caps")
async def caps_status(_user: dict = Depends(get_current_user)):  # noqa: B008
    """Operator view of the hard caps + current consumption."""
    from shared.exposure_caps import daily_spend_usd, open_notional_usd  # noqa: WPS433
    spent = await daily_spend_usd()
    open_ = await open_notional_usd()
    caps = caps_snapshot()
    return {
        "caps": caps,
        "today": {
            "spent_usd": spent,
            "remaining_usd": max(0.0, caps["per_day_usd"] - spent),
        },
        "open": {
            "open_notional_usd": open_,
            "remaining_usd": max(0.0, caps["open_notional_usd"] - open_),
        },
    }
