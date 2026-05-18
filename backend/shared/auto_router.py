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
    """Run gates + submit. Mirrors /execution/submit minus the JWT check.

    The orchestrator is intentionally linear: clamp → gate → submit →
    receipt → side-effects. Each phase delegates to a named helper
    defined below. Refactored 2026-05-17 from the original 194-line
    monolith; characterization-locked by
    tests/test_auto_router_helpers.py + the council diagnose tripwire.
    """
    intent_id = intent["intent_id"]
    notional_raw = float(intent.get("requested_notional_usd") or AUTO_ROUTER_NOTIONAL_USD)

    # Phase 1: clamp notional to the lane's per-order cap.
    notional, was_clamped = _clamp_notional_to_lane(notional_raw, intent.get("lane"))
    if was_clamped:
        logger.info(
            "auto_router clamping intent=%s lane=%s notional $%.2f → $%.2f (lane cap)",
            intent_id, intent.get("lane"), notional_raw, notional,
        )

    # Phase 2: run the gate chain.
    result = await _evaluate_gates(intent, notional)
    if result["verdict"] != "would_pass":
        await _persist_blocked_intent(intent_id, notional, result)
        return _blocked_response(intent_id, result["gates"])

    # Phase 3: compute effective notional after council risk multiplier.
    risk_multiplier = float(result.get("risk_multiplier") or 1.0)
    effective = _effective_notional(notional, risk_multiplier)
    side = _side_for_action(intent["action"])
    client_order_id = f"ar-{intent_id[:8]}-{uuid.uuid4().hex[:6]}"

    # Phase 4: submit to broker; handle the 3 outcome branches.
    try:
        order = await route_order(intent, notional_usd=effective, client_order_id=client_order_id)
    except BrokerRouteBlocked as e:
        await _persist_no_trade(intent_id, intent, str(e))
        return {"intent_id": intent_id, "verdict": "no_trade", "reason": str(e)}
    except Exception as e:  # noqa: BLE001
        await _persist_router_error(intent_id, intent, str(e))
        return {"intent_id": intent_id, "verdict": "error", "reason": str(e)}

    # Phase 5: build + persist the receipt.
    now = _now_iso()
    receipt = _build_receipt(
        intent=intent, order=order, side=side,
        effective_notional=effective, requested_notional=notional,
        risk_multiplier=risk_multiplier, gates=result["gates"], now_iso=now,
    )
    await _persist_executed_intent(intent_id, receipt, order, effective, notional, risk_multiplier, result["gates"], now)

    # Phase 6: audit + post-submit side effects (live position open, VRL).
    _audit_executed_to_shelly(intent, receipt, order, effective, notional, risk_multiplier)
    await _post_submit_side_effects(receipt, intent)

    return {
        "intent_id": intent_id,
        "verdict": "executed",
        "broker_order_id": order["order_id"],
        "symbol": intent.get("symbol"),
        "side": side,
        "notional_usd": effective,
        "risk_multiplier": risk_multiplier,
    }


# ── pure helpers (no IO, no async) ──────────────────────────────────────


def _clamp_notional_to_lane(notional: float, lane: Optional[str]) -> tuple[float, bool]:
    """Clamp `notional` to the per-order cap for `lane`. Returns the
    clamped value plus a bool indicating whether clamping fired.

    Doctrine: the default AUTO_ROUTER_NOTIONAL_USD ($100) blows past
    the crypto $30/order cap, so 100% of auto-routed crypto intents
    would fail at `cap_per_order_crypto`. Clamping pre-emptively keeps
    the gate chain useful instead of always tripping on the same rail.
    """
    from shared.exposure_caps import cap_for_lane  # noqa: WPS433
    lane_cap = cap_for_lane(lane)
    if notional > lane_cap:
        return lane_cap, True
    return notional, False


def _effective_notional(base: float, risk_multiplier: float) -> float:
    """Apply the council risk multiplier to the base notional. A zero
    or negative multiplier falls back to the base (paranoia — a hard
    block should have already been a gate failure)."""
    if risk_multiplier <= 0:
        return base
    return base * risk_multiplier


def _side_for_action(action: str) -> str:
    """Map an intent action to the broker-side string. BUY/COVER → BUY;
    everything else → SELL (legacy default for SHORT/HOLD)."""
    return "BUY" if action in ("BUY", "COVER") else "SELL"


def _blocked_response(intent_id: str, gates: list[dict]) -> dict:
    """Build the response envelope returned when the gate chain blocks."""
    first_block = next((g for g in gates if not g["passed"]), None)
    return {
        "intent_id": intent_id,
        "verdict": "blocked",
        "reason": first_block["reason"] if first_block else "gate chain blocked",
    }


def _build_receipt(
    *,
    intent: dict, order: dict, side: str,
    effective_notional: float, requested_notional: float,
    risk_multiplier: float, gates: list[dict], now_iso: str,
) -> dict:
    """Pure receipt builder. The schema mirrors the
    operator-confirmed path in shared/execution.py."""
    return {
        "receipt_id": str(uuid.uuid4()),
        "intent_id": intent["intent_id"],
        "stack": intent.get("stack"),
        "symbol": intent.get("symbol"),
        "canonical": order.get("canonical"),
        "lane": order.get("lane"),
        "broker_symbol": order.get("broker_symbol"),
        "action": intent.get("action"),
        "side": side,
        "notional_usd": effective_notional,
        "requested_notional_usd": requested_notional,
        "risk_multiplier": risk_multiplier,
        "broker": order.get("broker", "unknown"),
        "broker_order_id": order["order_id"],
        "client_order_id": order.get("client_order_id"),
        "status": order.get("status"),
        "submitted_at": order.get("submitted_at") or now_iso,
        "filled_at": order.get("filled_at"),
        "filled_qty": order.get("filled_qty", 0.0),
        "filled_avg_price": order.get("filled_avg_price"),
        "executed_at": now_iso,
        "executed_by": AUTO_ROUTER_EMAIL,
        "gates_passed": gates,
        "auto_routed": True,
        # MC receipt provenance — `mc_canonical_gate` mint + `broker_verify_receipt`.
        "mc_receipt": order.get("mc_receipt"),
        "mc_receipt_status": order.get("mc_receipt_status"),
        "mc_receipt_enforced": order.get("mc_receipt_enforced"),
    }


# ── persistence helpers (single-purpose Mongo IO) ──────────────────────


async def _persist_blocked_intent(intent_id: str, notional: float, result: dict) -> None:
    """Write the auto_router_blocked gate row + mark the intent blocked."""
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


async def _persist_no_trade(intent_id: str, intent: dict, reason: str) -> None:
    """Record a BrokerRouteBlocked NO_TRADE: gate row + shelly + intent."""
    await db[SHARED_GATE_RESULTS].insert_one({
        "intent_id": intent_id,
        "kind": "auto_router_no_trade",
        "ts": _now_iso(),
        "by": AUTO_ROUTER_EMAIL,
        "reason": reason,
    })
    record_async(
        event_type="order_rejected",
        brain=intent.get("stack"),
        symbol=intent.get("symbol"),
        action=intent.get("action"),
        outcome="no_trade",
        error_reason=reason,
        ref_id=intent_id,
    )
    await db[SHARED_INTENTS].update_one(
        {"intent_id": intent_id},
        {"$set": {
            "gate_state": "no_trade",
            "last_submit_ts": _now_iso(),
            "last_submit_by": AUTO_ROUTER_EMAIL,
            "no_trade_reason": reason,
        }},
    )


async def _persist_router_error(intent_id: str, intent: dict, error: str) -> None:
    """Record a generic broker-router exception."""
    await db[SHARED_GATE_RESULTS].insert_one({
        "intent_id": intent_id,
        "kind": "auto_router_error",
        "ts": _now_iso(),
        "by": AUTO_ROUTER_EMAIL,
        "error": error,
    })
    record_async(
        event_type="order_rejected",
        brain=intent.get("stack"),
        symbol=intent.get("symbol"),
        action=intent.get("action"),
        outcome="rejected",
        error_reason=error,
        ref_id=intent_id,
    )


async def _persist_executed_intent(
    intent_id: str, receipt: dict, order: dict,
    effective_notional: float, requested_notional: float,
    risk_multiplier: float, gates: list[dict], now: str,
) -> None:
    """Write the receipt + mark the intent executed + audit gate row."""
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
        "requested_notional_usd": requested_notional,
        "risk_multiplier": risk_multiplier,
        "broker_order_id": order["order_id"],
        "gates": gates,
    })


def _audit_executed_to_shelly(
    intent: dict, receipt: dict, order: dict,
    effective_notional: float, requested_notional: float, risk_multiplier: float,
) -> None:
    """Emit the order_routed event into mc_shelly for outcome learning."""
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
            "requested_notional_usd": requested_notional,
            "risk_multiplier": risk_multiplier,
            "status": order.get("status"),
            "auto_routed": True,
        },
    )


async def _post_submit_side_effects(receipt: dict, intent: dict) -> None:
    """Open the live-position lifecycle row + run VRL verification.
    Both are best-effort: failures here must not poison the executed
    response. Mirrors the operator-confirmed path in shared/execution.py.
    """
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
