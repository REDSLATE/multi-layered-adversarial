"""Auto-router — paper-trading mode.

Periodically scans `shared_intents` for unexecuted, routable intents
that pass the full gate chain, and submits them to the broker. This
exists so the brains can trade freely on paper without the operator
clicking Submit on every single intent.

Doctrine:
  * Reads the same gate chain as the manual /execution/submit endpoint
    (`shared.execution._evaluate_gates`) — no parallel safety logic.
  * Order notional defaults to AUTO_ROUTER_NOTIONAL_USD per intent.
    Each intent can override via `intent.requested_notional_usd`.
  * Per-intent idempotency: the `executed=true` flag on `shared_intents`
    prevents double-fires; this loop simply filters `executed != true`.
  * Routes attribution to a synthetic operator email so receipts can be
    distinguished from operator-clicked fills.
  * Tick interval & enable flag come from env so they can be tuned
    without code change.

Disable with: AUTO_ROUTER_ENABLED=false in backend/.env.
"""
from __future__ import annotations

import asyncio
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Optional

from db import db
from namespaces import EXECUTION_RECEIPTS, SHARED_GATE_RESULTS, SHARED_INTENTS
from shared.broker.alpaca_routes import get_alpaca_adapter
from shared.broker_router import (
    BrokerRouteBlocked,
    adapter_for_lane,
    route_order,
)
from shared.execution import _evaluate_gates
from shared.mc_shelly import record_async


logger = logging.getLogger("auto_router")

# Loop tunables — env-driven so we can poke them without redeploys.
AUTO_ROUTER_ENABLED = os.environ.get("AUTO_ROUTER_ENABLED", "true").lower() == "true"
AUTO_ROUTER_INTERVAL_SEC = int(os.environ.get("AUTO_ROUTER_INTERVAL_SEC", "30"))
AUTO_ROUTER_NOTIONAL_USD = float(os.environ.get("AUTO_ROUTER_NOTIONAL_USD", "100"))
AUTO_ROUTER_MAX_PER_TICK = int(os.environ.get("AUTO_ROUTER_MAX_PER_TICK", "5"))
AUTO_ROUTER_EMAIL = "auto-router@mission-control"

_TASK: Optional[asyncio.Task] = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _route_one(intent: dict) -> dict:
    """Run gates + submit. Mirrors /execution/submit minus the JWT check."""
    intent_id = intent["intent_id"]
    notional = float(intent.get("requested_notional_usd") or AUTO_ROUTER_NOTIONAL_USD)

    # Lane-aware notional clamp. The default AUTO_ROUTER_NOTIONAL_USD
    # ($100) blows past the crypto $30/order cap, which caused 100% of
    # auto-routed crypto intents to NO_TRADE on the per-order cap.
    # Clamp to the lane's effective cap so crypto intents actually
    # execute on Kraken instead of silently dying at the gate.
    from shared.exposure_caps import cap_for_lane  # noqa: WPS433
    lane_cap = cap_for_lane(intent.get("lane"))
    if notional > lane_cap:
        logger.info(
            "auto_router clamping intent=%s lane=%s notional $%.2f → $%.2f (lane cap)",
            intent_id, intent.get("lane"), notional, lane_cap,
        )
        notional = lane_cap

    result = await _evaluate_gates(intent, notional)
    if result["verdict"] != "would_pass":
        await db[SHARED_GATE_RESULTS].insert_one({
            "intent_id": intent_id,
            "kind": "auto_router_blocked",
            "ts": _now_iso(),
            "by": AUTO_ROUTER_EMAIL,
            "order_notional_usd": notional,
            "verdict": result["verdict"],
            "gates": result["gates"],
            "risk_multiplier": result.get("risk_multiplier"),
        })
        await db[SHARED_INTENTS].update_one(
            {"intent_id": intent_id},
            {"$set": {
                "gate_state": "blocked",
                "last_submit_ts": _now_iso(),
                "last_submit_by": AUTO_ROUTER_EMAIL,
            }},
        )
        first_block = next((g for g in result["gates"] if not g["passed"]), None)
        return {
            "intent_id": intent_id,
            "verdict": "blocked",
            "reason": first_block["reason"] if first_block else "gate chain blocked",
        }

    # Council-driven risk multiplier (1.0 if no dissent, 0.5 on
    # EXECUTOR_OVERRIDES_SOFT_DISSENT). The broker sees the reduced
    # notional; gate caps were already evaluated against this value.
    risk_multiplier = float(result.get("risk_multiplier") or 1.0)
    effective_notional = notional * risk_multiplier if risk_multiplier > 0 else notional

    side = "BUY" if intent["action"] in ("BUY", "COVER") else "SELL"
    client_order_id = f"ar-{intent_id[:8]}-{uuid.uuid4().hex[:6]}"

    try:
        order = await route_order(intent, notional_usd=effective_notional, client_order_id=client_order_id)
    except BrokerRouteBlocked as e:
        # NO_TRADE: fail-closed at the resolver/router boundary.
        await db[SHARED_GATE_RESULTS].insert_one({
            "intent_id": intent_id,
            "kind": "auto_router_no_trade",
            "ts": _now_iso(),
            "by": AUTO_ROUTER_EMAIL,
            "reason": str(e),
        })
        record_async(
            event_type="order_rejected",
            brain=intent.get("stack"),
            symbol=intent.get("symbol"),
            action=intent.get("action"),
            outcome="no_trade",
            error_reason=str(e),
            ref_id=intent_id,
        )
        await db[SHARED_INTENTS].update_one(
            {"intent_id": intent_id},
            {"$set": {
                "gate_state": "no_trade",
                "last_submit_ts": _now_iso(),
                "last_submit_by": AUTO_ROUTER_EMAIL,
                "no_trade_reason": str(e),
            }},
        )
        return {"intent_id": intent_id, "verdict": "no_trade", "reason": str(e)}
    except Exception as e:  # noqa: BLE001
        await db[SHARED_GATE_RESULTS].insert_one({
            "intent_id": intent_id,
            "kind": "auto_router_error",
            "ts": _now_iso(),
            "by": AUTO_ROUTER_EMAIL,
            "error": str(e),
        })
        record_async(
            event_type="order_rejected",
            brain=intent.get("stack"),
            symbol=intent.get("symbol"),
            action=intent.get("action"),
            outcome="rejected",
            error_reason=str(e),
            ref_id=intent_id,
        )
        return {"intent_id": intent_id, "verdict": "error", "reason": str(e)}

    now = _now_iso()
    receipt = {
        "receipt_id": str(uuid.uuid4()),
        "intent_id": intent_id,
        "stack": intent.get("stack"),
        "symbol": intent.get("symbol"),
        "canonical": order.get("canonical"),
        "lane": order.get("lane"),
        "broker_symbol": order.get("broker_symbol"),
        "action": intent.get("action"),
        "side": side,
        "notional_usd": effective_notional,
        "requested_notional_usd": notional,
        "risk_multiplier": risk_multiplier,
        "broker": order.get("broker", "unknown"),
        "broker_order_id": order["order_id"],
        "client_order_id": order.get("client_order_id"),
        "status": order.get("status"),
        "submitted_at": order.get("submitted_at") or now,
        "filled_at": order.get("filled_at"),
        "filled_qty": order.get("filled_qty", 0.0),
        "filled_avg_price": order.get("filled_avg_price"),
        "executed_at": now,
        "executed_by": AUTO_ROUTER_EMAIL,
        "gates_passed": result["gates"],
        "auto_routed": True,
    }
    await db[EXECUTION_RECEIPTS].insert_one(receipt)
    await db[SHARED_INTENTS].update_one(
        {"intent_id": intent_id},
        {"$set": {
            "executed": True,
            "executed_at": now,
            "execution_receipt_id": receipt["receipt_id"],
            "broker_order_id": order["order_id"],
            "gate_state": "passed",
            "last_submit_ts": now,
            "last_submit_by": AUTO_ROUTER_EMAIL,
        }},
    )
    await db[SHARED_GATE_RESULTS].insert_one({
        "intent_id": intent_id,
        "kind": "auto_router_passed",
        "ts": now,
        "by": AUTO_ROUTER_EMAIL,
        "order_notional_usd": effective_notional,
        "requested_notional_usd": notional,
        "risk_multiplier": risk_multiplier,
        "broker_order_id": order["order_id"],
        "gates": result["gates"],
    })
    record_async(
        event_type="order_routed",
        brain=intent.get("stack"),
        symbol=intent.get("symbol"),
        action=intent.get("action"),
        outcome="executed",
        ref_id=receipt["receipt_id"],
        extra={
            "broker_order_id": order["order_id"],
            "notional_usd": effective_notional,
            "requested_notional_usd": notional,
            "risk_multiplier": risk_multiplier,
            "status": order.get("status"),
            "auto_routed": True,
        },
    )

    # Live-position lifecycle (2026-02-16) — open a tracked position
    # against this auto-routed receipt. Same idempotent contract as the
    # operator-confirmed path in shared/execution.py.
    try:
        from shared.live_positions import open_from_receipt as _open_pos  # noqa: WPS433
        await _open_pos(receipt, intent=intent)
    except Exception as e:  # noqa: BLE001
        logger.warning("auto_router: live_positions.open_from_receipt failed: %s", e)
    try:
        from shared.vrl import verify_receipt as _verify  # noqa: WPS433
        await _verify(receipt, intent=intent)
    except Exception as e:  # noqa: BLE001
        logger.warning("auto_router: vrl.verify_receipt failed: %s", e)

    return {
        "intent_id": intent_id,
        "verdict": "executed",
        "broker_order_id": order["order_id"],
        "symbol": intent.get("symbol"),
        "side": side,
        "notional_usd": effective_notional,
        "risk_multiplier": risk_multiplier,
    }


async def _tick() -> list[dict]:
    """One scan pass. Picks up at most AUTO_ROUTER_MAX_PER_TICK eligible intents."""
    q = {
        "executed": {"$ne": True},
        "action": {"$in": ["BUY", "SELL", "SHORT", "COVER"]},
        "symbol": {"$ne": None},
        "holds_executor_seat": True,
    }
    intents = await db[SHARED_INTENTS].find(q, {"_id": 0}).sort("created_at", 1).to_list(AUTO_ROUTER_MAX_PER_TICK)
    if not intents:
        return []
    results = []
    for intent in intents:
        try:
            r = await _route_one(intent)
            results.append(r)
            if r.get("verdict") == "executed":
                logger.info(
                    "auto-routed %s %s %s $%.2f -> %s",
                    intent.get("stack"), intent.get("action"), intent.get("symbol"),
                    r.get("notional_usd", 0), r.get("broker_order_id"),
                )
        except Exception as e:  # noqa: BLE001
            logger.exception("auto-router error on intent %s: %s", intent.get("intent_id"), e)
    return results


async def _loop() -> None:
    logger.info(
        "auto-router started: interval=%ss notional=$%s max_per_tick=%s",
        AUTO_ROUTER_INTERVAL_SEC, AUTO_ROUTER_NOTIONAL_USD, AUTO_ROUTER_MAX_PER_TICK,
    )
    while True:
        try:
            await _tick()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.exception("auto-router tick failed: %s", e)
        await asyncio.sleep(AUTO_ROUTER_INTERVAL_SEC)


def start_auto_router_if_enabled() -> None:
    global _TASK
    if not AUTO_ROUTER_ENABLED:
        logger.info("auto-router disabled (AUTO_ROUTER_ENABLED=false)")
        return
    if _TASK and not _TASK.done():
        return
    loop = asyncio.get_event_loop()
    _TASK = loop.create_task(_loop())


async def stop_auto_router() -> None:
    global _TASK
    if _TASK and not _TASK.done():
        _TASK.cancel()
        try:
            await _TASK
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    _TASK = None
