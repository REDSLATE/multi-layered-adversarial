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
from shared.intent_contract import classify_brain_intent
from shared.mc_shelly import record_async


logger = logging.getLogger("auto_router")

# Loop tunables — env-driven so we can poke them without redeploys.
AUTO_ROUTER_ENABLED = os.environ.get("AUTO_ROUTER_ENABLED", "true").lower() == "true"
AUTO_ROUTER_INTERVAL_SEC = int(os.environ.get("AUTO_ROUTER_INTERVAL_SEC", "30"))
# 2026-02-17 (pass #51, operator "let it rip" config): default lowered
# from $100 → $10. First-light autonomous trades are tiny so a bad
# signal costs lunch, not rent. Operator can lift via env var or by
# stamping `requested_notional_usd` on the intent itself.
AUTO_ROUTER_NOTIONAL_USD = float(os.environ.get("AUTO_ROUTER_NOTIONAL_USD", "10"))

# Per-tick rate cap. Doctrine (verified 2026-06-10 against handoff
# pass-13's "is this obsolete now that in-flight dedupe is live?"):
#
#   This cap is NOT obsolete and is NOT redundant with
#   `shared/in_flight_orders.py`. They solve DIFFERENT problems:
#
#     * AUTO_ROUTER_MAX_PER_TICK = rate cap. With 30s ticks at 5/tick,
#       max sustained ~10 orders/min. Protects against:
#         - Broker rate limits (Public, Kraken, Webull each have their
#           own quotas; bunching can trip throttles)
#         - Sudden P&L exposure if 50 intents queue up after a feed
#           outage clears (the burst is spread across 10 ticks)
#         - Webull pilot constraint: at $3-$10/order, 50 simultaneous
#           orders means $150-$500 hits the wire in 30s; spreading it
#           gives the operator a chance to see and intervene
#
#     * in_flight_orders.claim() = duplicate prevention. Stops the
#       SAME (symbol, brain, side) intent from being submitted twice
#       while one is in flight. This is what killed the 2026-06-09
#       amnesia loop where the router was re-picking the SAME row
#       under contention.
#
#   Removing this cap on the assumption that dedupe covers it would
#   re-expose the broker rate-limit and operator-visibility surfaces.
#   `tests/test_auto_router_max_per_tick.py` pins the contract.
AUTO_ROUTER_MAX_PER_TICK = int(os.environ.get("AUTO_ROUTER_MAX_PER_TICK", "5"))
AUTO_ROUTER_EMAIL = "auto-router@mission-control"

_TASK: Optional[asyncio.Task] = None

# ── Loop heartbeat / introspection (2026-06-09) ──────────────────
# The auto-router is the single most operationally-critical loop in
# MC — when it's silent the entire fleet falls back to dry-runs only.
# These module-level counters let `/api/admin/auto-router/status`
# surface the task's liveness without restarting the pod.
_TICK_COUNT: int = 0
_LAST_TICK_TS: Optional[str] = None
_LAST_TICK_RESULTS: int = 0
_LAST_TICK_EXECUTED: int = 0
_LAST_TICK_ERROR: Optional[str] = None
_STARTED_AT: Optional[str] = None


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

    # ── Phase 4 (2026-02-17): LADDER-FIRST routing ────────────────────
    # The ladder stage (per brain × lane) is now AUTHORITATIVE. We
    # resolve it BEFORE the advisory_only classifier so a brain's
    # self-zero claim no longer gets to bypass the shadow path
    # asymmetrically. Doctrine: brains are signal sources; MC owns
    # capital deployment.
    #
    # Old behavior (Phase 3 / "observation observed but not enforced"):
    #   - brain emits `size_multiplier=0` → observation_receipt only
    #   - brain emits `size_multiplier>0` → broker fill
    #   This created the Alpha-vs-others split the operator hit on
    #   prod: Alpha never self-zeroed so its intents fired through
    #   while Camaro/Chevelle/REDEYE intents got permanently shadowed.
    #
    # New behavior (Phase 4 ENGAGED):
    #   stage = observation_only → observation_receipt, no broker fill
    #                              (regardless of self-zero)
    #   stage = micro_paper      → broker fill @ LADDER_MICRO_PAPER_USD,
    #                              receipt tagged execution_mode=ladder_paper
    #   stage = micro_live       → broker fill @ LADDER_MICRO_LIVE_USD,
    #                              receipt tagged execution_mode=ladder_live_micro
    #   stage = normal_live      → full sizing path
    #
    # Stage promotions happen via /api/admin/learning-ladder/promote
    # (auto-eligible at 100 obs / 0.55 win-rate; 50 paper / 0.30 R).
    intent_lane = str(intent.get("lane") or "").lower()
    brain = str(intent.get("stack") or "").lower()
    from shared.sizing_gate import (  # noqa: WPS433
        ROUTE_OBSERVE,
        evaluate_sizing_with_ladder,
    )
    sizing = await evaluate_sizing_with_ladder(notional_raw, brain, intent_lane)

    # Phase 0: MC classifier — "is this even an executable candidate?"
    # Sidecars speak in their own shape (BUY/SELL/HOLD, opinions,
    # authority calls). MC owns the classification. HOLD, missing
    # symbol, unknown direction, below-floor confidence, missing lane
    # → advisory_only=True. Persist a typed ledger row and skip
    # routing entirely. This stops HOLDs (and other non-directional
    # noise) from spamming the gate chain.
    #
    # Lane-specific exec floor: crypto 0.30, equity 0.30 (operator
    # spec). Adjust here if you want different floors per lane.
    # 2026-02-17 (pass #51, operator "let it rip" config): floor
    # lowered from 0.35 → 0.30. Lets the auto-router fire on more
    # mid-conviction signals while patents are suspended. Shadow-
    # learning floor (observation receipts) is still 0.30, so this
    # change effectively merges the two; the lane toggle is the
    # operator's master kill if signals get noisy.
    min_exec_conf = float(os.environ.get("RISEDUAL_EXEC_CONFIDENCE_FLOOR", "0.30"))
    classification = classify_brain_intent(intent, min_exec_conf=min_exec_conf)

    # ── LADDER GATE ELIMINATED 2026-06-10 (operator directive) ──
    # The previous ladder-first shadow gate that lived here (Phase 4)
    # rerouted every observation_only intent to an observation
    # receipt and skipped the broker. Per `_ladder_cap_and_route`
    # in `shared/sizing_gate.py`, all stages now resolve to
    # `live_normal`, so this branch is structurally unreachable —
    # we leave a defensive assertion-style check + a warning so any
    # accidental regression of `sizing.route == ROUTE_OBSERVE` is
    # surfaced loudly instead of silently shadowing trades. Other
    # safety rails (lane toggle, broker freeze, broker caps,
    # exposure cap, in-flight dedupe, MC receipt seal, position
    # misread detector) stay 100% active.
    if sizing.route == ROUTE_OBSERVE:
        logger.error(
            "auto_router LADDER_REGRESSION intent=%s brain=%s lane=%s stage=%s "
            "— sizing returned ROUTE_OBSERVE but the ladder gate was eliminated; "
            "this should be unreachable. NO_TRADE.",
            intent_id, brain, intent_lane, sizing.stage,
        )
        return {
            "intent_id": intent_id,
            "verdict": "no_trade",
            "reason": "ladder_regression_unreachable_branch",
            "execution_ready": False,
        }

    if classification.advisory_only:
        # Stage is above observation_only but the intent itself is
        # non-directional (HOLD / missing fields). Still classify
        # advisory; no broker, no observation receipt.
        logger.info(
            "auto_router skip intent=%s brain=%s lane=%s symbol=%s "
            "advisory_only reason=%s",
            intent_id, classification.brain, intent_lane,
            classification.symbol, classification.reason,
        )
        await _persist_advisory_classification(intent_id, intent, classification)
        return {
            "intent_id": intent_id,
            "verdict": "advisory_only",
            "reason": classification.reason,
            "execution_ready": False,
        }

    # Phase 1: clamp notional via the SIZING GATE.
    # Doctrine pin (2026-05-26): the sizing gate evaluates BOTH the
    # engineering lane cap AND the operator's micro_live rail, then
    # binds to whichever is tighter. Provenance stamped on the
    # decision so receipts carry the audit trail. When MICRO_LIVE is
    # enabled the cap is typically $5/order — small enough that a
    # brain mistake costs lunch, not rent.
    #
    # Phase 4 ENGAGED (2026-02-17): `sizing` was computed up top via
    # evaluate_sizing_with_ladder so the ladder cap is already
    # folded in. We just unpack final_usd here.
    notional = sizing.final_usd
    if sizing.was_clamped:
        logger.info(
            "auto_router sizing intent=%s lane=%s req $%.2f → final $%.2f rail=%s "
            "lane_cap=$%.2f micro_live=$%s ladder=$%s stage=%s route=%s",
            intent_id, intent.get("lane"), sizing.requested_usd, sizing.final_usd,
            sizing.binding_rail, sizing.lane_cap_usd,
            f"{sizing.micro_live_cap_usd:.2f}" if sizing.micro_live_cap_usd else "off",
            f"{sizing.ladder_cap_usd:.2f}" if sizing.ladder_cap_usd is not None else "none",
            sizing.stage, sizing.route,
        )
    if notional <= 0:
        await _persist_no_trade(intent_id, intent, f"sizing_gate_zero:{sizing.binding_rail}")
        return {"intent_id": intent_id, "verdict": "no_trade", "reason": f"sizing_gate_zero:{sizing.binding_rail}"}

    # Phase 1b: RUNTIME KILL SWITCH (2026-05-26). Operator-flipped
    # Mongo doc takes precedence over env. Fail-CLOSED on read errors.
    from routes.trading_controls import is_trading_enabled  # noqa: WPS433
    if not await is_trading_enabled():
        await _persist_no_trade(intent_id, intent, "trading_controls_disabled")
        return {"intent_id": intent_id, "verdict": "no_trade", "reason": "trading_controls_disabled"}

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

    # Phase 3b (2026-06-10, post-AAPL incident): IN-FLIGHT ORDER DEDUPE.
    # The AAPL 06-09 runaway loop happened because MC submitted 130
    # BUYs in 13 minutes — every successive auto-router tick saw the
    # position context as FLAT because Public.com's fill index lagged
    # the broker ack. The two-layer dedupe below closes that window:
    #
    #   Layer A (broker truth):  has_pending_order(symbol)
    #     "Did Public.com index a fill within the last 30s?" — yes
    #     means an order is already executing, refuse to send another.
    #
    #   Layer B (pre-ack lock):  claim_in_flight_slot(symbol)
    #     In-memory pending set capturing the gap BEFORE Public.com
    #     indexes the fill. Released on broker reject/error; on
    #     success it ages out (and is shadowed by Layer A anyway).
    #
    # Doctrine: only 1 order per symbol can be in flight at a time.
    # If either layer says "in flight," refuse to submit. This is
    # the structural fix for the 130-trade amnesia loop.
    symbol_for_dedupe = (intent.get("symbol") or "").upper()
    if symbol_for_dedupe:
        from shared.broker_fills import has_pending_order  # noqa: WPS433
        from shared.in_flight_orders import (  # noqa: WPS433
            claim_in_flight_slot,
            release_in_flight_slot,
        )
        if await has_pending_order(symbol_for_dedupe):
            reason = "in_flight_dedupe:broker_fill_within_ttl"
            await _persist_no_trade(intent_id, intent, reason)
            logger.info(
                "auto_router DEDUPE_BLOCK intent=%s symbol=%s reason=%s",
                intent_id, symbol_for_dedupe, reason,
            )
            return {"intent_id": intent_id, "verdict": "no_trade", "reason": reason}
        if not await claim_in_flight_slot(symbol_for_dedupe, intent_id=intent_id):
            reason = "in_flight_dedupe:pending_submission"
            await _persist_no_trade(intent_id, intent, reason)
            logger.info(
                "auto_router DEDUPE_BLOCK intent=%s symbol=%s reason=%s",
                intent_id, symbol_for_dedupe, reason,
            )
            return {"intent_id": intent_id, "verdict": "no_trade", "reason": reason}
    else:
        release_in_flight_slot = None  # type: ignore  # appease ruff

    # Phase 4: submit to broker; handle the 3 outcome branches.
    # 2026-02-19: timeout-wrap the broker submit. Even with SDK calls
    # now isolated on a thread executor (shared/broker/webull.py
    # `_sdk_call`), a hung remote can still tie up an executor slot
    # indefinitely. A 25-second ceiling keeps the tick bounded —
    # well under the 30s `AUTO_ROUTER_INTERVAL_SEC` so a stalled
    # call can't block the next tick.
    try:
        order = await asyncio.wait_for(
            route_order(intent, notional_usd=effective, client_order_id=client_order_id),
            timeout=25.0,
        )
    except asyncio.TimeoutError:
        if symbol_for_dedupe and release_in_flight_slot is not None:
            await release_in_flight_slot(symbol_for_dedupe)
        await _persist_router_error(intent_id, intent, "broker_submit_timeout_25s")
        return {"intent_id": intent_id, "verdict": "error", "reason": "broker_submit_timeout_25s"}
    except BrokerRouteBlocked as e:
        if symbol_for_dedupe and release_in_flight_slot is not None:
            await release_in_flight_slot(symbol_for_dedupe)
        await _persist_no_trade(intent_id, intent, str(e))
        return {"intent_id": intent_id, "verdict": "no_trade", "reason": str(e)}
    except Exception as e:  # noqa: BLE001
        if symbol_for_dedupe and release_in_flight_slot is not None:
            await release_in_flight_slot(symbol_for_dedupe)
        await _persist_router_error(intent_id, intent, str(e))
        return {"intent_id": intent_id, "verdict": "error", "reason": str(e)}

    # Phase 4b (2026-06-10, P1): invalidate the position-context cache
    # for this lane the instant the broker accepts the order. Doctrine:
    # the position cache TTL is 2s (was 10s) — short enough that
    # steady-state staleness is bounded, but the broker indexes fills
    # in ~500ms. By punching the cache here we guarantee the NEXT
    # brain tick on this lane re-fetches fresh broker state, closing
    # the last remaining amnesia window. Safe failure: invalidate is
    # a dict.pop() — can't raise.
    try:
        from shared.position_context import invalidate_for_lane  # noqa: WPS433
        lane_for_cache = (intent.get("lane") or "").lower()
        if lane_for_cache:
            invalidate_for_lane(lane_for_cache)
    except Exception:  # noqa: BLE001
        pass

    # Phase 5: build + persist the receipt.
    now = _now_iso()
    receipt = _build_receipt(
        intent=intent, order=order, side=side,
        effective_notional=effective, requested_notional=notional,
        risk_multiplier=risk_multiplier, gates=result["gates"], now_iso=now,
    )
    # Stamp sizing provenance — micro_live + Phase 4 ladder audit trail.
    receipt["sizing_provenance"] = {
        "requested_usd": sizing.requested_usd,
        "final_usd": sizing.final_usd,
        "was_clamped": sizing.was_clamped,
        "binding_rail": sizing.binding_rail,
        "micro_live_enabled": sizing.micro_live_enabled,
        "lane_cap_usd": sizing.lane_cap_usd,
        "micro_live_cap_usd": sizing.micro_live_cap_usd,
        # Phase 4 ladder fields (2026-02-17). `execution_mode` is
        # ALSO copied to the top-level receipt below so
        # `learning_ladder._paper_progress` can count fills with a
        # simple {"execution_mode": "ladder_paper"} filter.
        "stage": sizing.stage,
        "route": sizing.route,
        "ladder_cap_usd": sizing.ladder_cap_usd,
        "execution_mode": sizing.execution_mode,
    }
    # Phase 4: top-level tag so the ladder unlock counter (and any
    # other downstream slicer) can filter without digging into the
    # provenance sub-doc.
    if sizing.execution_mode:
        receipt["execution_mode"] = sizing.execution_mode
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


async def _persist_advisory_classification(
    intent_id: str,
    intent: dict,
    classification,
) -> None:
    """Phase-0 ledger row: a brain emission that's NOT an executable
    candidate (HOLD, opinion, missing lane/symbol, below-floor conf).
    Persisted so the operator can audit WHY it was skipped, without
    polluting the gate-result ledger with a fake 'blocked' row."""
    now = _now_iso()
    await db[SHARED_GATE_RESULTS].insert_one({
        "intent_id": intent_id,
        "kind": "auto_router_advisory_only",
        "ts": now,
        "by": AUTO_ROUTER_EMAIL,
        "classification": {
            "executable_candidate": classification.executable_candidate,
            "advisory_only": classification.advisory_only,
            "reason": classification.reason,
            "normalized_direction": classification.normalized_direction,
            "confidence": classification.confidence,
            "lane": classification.lane,
            "symbol": classification.symbol,
            "brain": classification.brain,
        },
    })
    await db[SHARED_INTENTS].update_one(
        {"intent_id": intent_id},
        {"$set": {
            "gate_state": "advisory_only",
            "execution_ready": False,
            "advisory_reason": classification.reason,
            "last_submit_ts": now,
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


async def _sweep_seat_mismatched_intents() -> int:
    """Doctrine (2026-05-31, position-model alignment): an intent's
    `holds_executor_seat=False` flag means "the brain that POSTED this
    intent did not hold the executor seat at post-time". Under the
    position-model doctrine (2026-05-28), that's NO LONGER a terminal
    state — authority lives in the seat, not the brain, so whichever
    brain CURRENTLY holds the lane's executor seat can route this
    intent. The `executor_seat_check` gate has already been relaxed to
    reflect this.

    The OLD sweep would mark every such intent `gate_state=blocked`
    on a 30-second tick — a silent garbage collector that killed
    intents the gate would have passed. That's the actual "line to
    execute trades is broken" symptom the operator surfaced.

    The NEW sweep is consistent with the position-model gate:
      - If the lane has a current executor-seat holder → leave the
        intent pending; the auto-router will pick it up.
      - If the lane has NO current executor-seat holder → block it
        with a typed reason ("no seat-holder for lane=X") so the
        operator queue stays honest. Operator can re-seat and the
        next post will succeed; existing blocked intents stay blocked
        for audit clarity rather than silently flipping back.

    This is symmetric with the `_evaluate_gates::executor_seat_check`
    branch logic — same question, same answer.
    """
    from shared.executor_seat import get_seat_holder, seats_with_execute  # noqa: WPS433

    q = {
        "gate_state": "pending",
        "executed": {"$ne": True},
        "action": {"$in": ["BUY", "SELL", "SHORT", "COVER"]},
        "holds_executor_seat": False,
    }
    now = _now_iso()
    candidates = await db[SHARED_INTENTS].find(
        q, {"_id": 0, "intent_id": 1, "stack": 1, "symbol": 1,
            "action": 1, "lane": 1, "executor_holder_at_post": 1},
    ).limit(500).to_list(500)
    if not candidates:
        return 0

    # Cache per-lane seat-occupancy across the candidate sweep so we
    # don't hammer the seat collection for every intent in a batch.
    lane_has_holder: dict[str, bool] = {}

    async def _lane_has_seat(lane: str) -> bool:
        if lane in lane_has_holder:
            return lane_has_holder[lane]
        eligible = seats_with_execute(lane)
        for seat_name in eligible:
            if await get_seat_holder(seat_name):
                lane_has_holder[lane] = True
                return True
        lane_has_holder[lane] = False
        return False

    blocked_count = 0
    for it in candidates:
        lane = (it.get("lane") or "").lower()
        if await _lane_has_seat(lane):
            # Position-model: someone holds the seat → intent is
            # eligible to fire under the relaxed gate. Leave it
            # pending; _tick() will pick it up on the next pass.
            continue
        # No holder anywhere → terminal block with a clear, lane-aware
        # reason. Different reason text than the old brain-coupled
        # message so audits show the doctrine change.
        await _persist_blocked_intent(
            it["intent_id"], 0.0, {
                "verdict": "blocked",
                "gates": [{
                    "name": "executor_seat_check",
                    "passed": False,
                    "reason": (
                        f"no current executor-seat holder for lane="
                        f"{lane or 'unknown'!r}; intent posted by "
                        f"{it.get('stack')!r} when seat was held by "
                        f"{it.get('executor_holder_at_post')!r} — "
                        f"swept by auto_router at {now}"
                    ),
                }],
                "risk_multiplier": 0.0,
            },
        )
        blocked_count += 1
    if blocked_count:
        logger.info(
            "auto_router: swept %d seat-mismatched limbo intents (no current holder)",
            blocked_count,
        )
    return blocked_count


async def _tick() -> list[dict]:
    """One scan pass. Picks up at most AUTO_ROUTER_MAX_PER_TICK eligible intents.

    Also runs the seat-mismatch sweep at most once per tick so the
    legacy limbo queue drains over time without flooding mongo on
    every cycle.

    Position-model pickup (2026-05-31): the query no longer filters on
    `holds_executor_seat=True` (the brain-coupled "did the poster hold
    the seat at post-time" flag). Instead, eligibility is checked
    PER-INTENT against the current seat-holder for the intent's lane —
    same question the gate chain now asks. This means an intent posted
    by REDEYE while Alpha held the equity executor seat IS eligible to
    fire as long as some brain currently holds an equity executor seat,
    regardless of who that brain is.
    """
    from shared.executor_seat import get_seat_holder, seats_with_execute  # noqa: WPS433

    # Drain seat-mismatch limbo first (cheap when empty).
    await _sweep_seat_mismatched_intents()
    q = {
        "executed": {"$ne": True},
        "action": {"$in": ["BUY", "SELL", "SHORT", "COVER"]},
        "symbol": {"$ne": None},
        # Honest queue: don't re-process intents already terminally
        # blocked by an earlier tick. Without this, the auto_router
        # would keep retrying gate-failed intents forever (the noise
        # the operator saw in the TRAINING feed).
        "gate_state": {"$nin": ["blocked", "no_trade", "advisory_only"]},
    }
    # Pull a larger sample than AUTO_ROUTER_MAX_PER_TICK so the
    # per-intent position-model filter can drop ineligible ones and
    # still leave us with up-to-MAX_PER_TICK eligible candidates.
    sample = await (
        db[SHARED_INTENTS]
        .find(q, {"_id": 0})
        .sort("created_at", 1)
        .to_list(AUTO_ROUTER_MAX_PER_TICK * 4)
    )
    if not sample:
        return []

    # Per-lane seat-occupancy cache for the duration of this tick.
    lane_has_holder: dict[str, bool] = {}

    async def _lane_eligible(lane: str) -> bool:
        if lane in lane_has_holder:
            return lane_has_holder[lane]
        eligible = seats_with_execute(lane)
        for seat_name in eligible:
            if await get_seat_holder(seat_name):
                lane_has_holder[lane] = True
                return True
        lane_has_holder[lane] = False
        return False

    intents: list[dict] = []
    for it in sample:
        if len(intents) >= AUTO_ROUTER_MAX_PER_TICK:
            break
        lane = (it.get("lane") or "").lower()
        if not await _lane_eligible(lane):
            # No current seat-holder for this lane — gate would fail
            # `executor_seat_check` anyway. Skip silently; the sweep
            # has already terminally-blocked these.
            continue
        intents.append(it)

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
    global _STARTED_AT, _TICK_COUNT, _LAST_TICK_TS, _LAST_TICK_RESULTS, _LAST_TICK_EXECUTED, _LAST_TICK_ERROR
    _STARTED_AT = _now_iso()
    logger.info(
        "auto-router started: interval=%ss notional=$%s max_per_tick=%s",
        AUTO_ROUTER_INTERVAL_SEC, AUTO_ROUTER_NOTIONAL_USD, AUTO_ROUTER_MAX_PER_TICK,
    )
    while True:
        try:
            results = await _tick()
            _TICK_COUNT += 1
            _LAST_TICK_TS = _now_iso()
            _LAST_TICK_RESULTS = len(results) if results else 0
            _LAST_TICK_EXECUTED = sum(
                1 for r in (results or []) if r.get("verdict") == "executed"
            )
            _LAST_TICK_ERROR = None
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            _LAST_TICK_ERROR = f"{type(e).__name__}: {e}"
            logger.exception("auto-router tick failed: %s", e)
        await asyncio.sleep(AUTO_ROUTER_INTERVAL_SEC)


def get_status() -> dict:
    """Read-only snapshot of the auto-router task. Surfaced via
    `GET /api/admin/auto-router/status` so the operator can answer
    "is the loop actually running?" without restarting the pod or
    grepping logs. Doctrine: this MUST be cheap and read-only —
    never touch broker state from a diagnostic."""
    task_done = bool(_TASK is None or _TASK.done())
    task_alive = bool(_TASK is not None and not _TASK.done())
    return {
        "enabled_env": AUTO_ROUTER_ENABLED,
        "task_alive": task_alive,
        "task_done": task_done,
        "task_exception": (
            repr(_TASK.exception()) if (_TASK and _TASK.done() and not _TASK.cancelled())
            else None
        ) if _TASK and _TASK.done() else None,
        "interval_sec": AUTO_ROUTER_INTERVAL_SEC,
        "default_notional_usd": AUTO_ROUTER_NOTIONAL_USD,
        "max_per_tick": AUTO_ROUTER_MAX_PER_TICK,
        "started_at": _STARTED_AT,
        "tick_count": _TICK_COUNT,
        "last_tick_ts": _LAST_TICK_TS,
        "last_tick_results": _LAST_TICK_RESULTS,
        "last_tick_executed": _LAST_TICK_EXECUTED,
        "last_tick_error": _LAST_TICK_ERROR,
        "now": _now_iso(),
        "doctrine_note": (
            "The auto-router is the ONLY loop that turns BUY/SELL "
            "intents into broker calls. If `task_alive=false`, no "
            "intent will ever execute autonomously — only manual "
            "/api/execution/submit calls work. If `task_alive=true` "
            "but `last_tick_ts` is stale (older than ~2× interval_sec), "
            "the tick is stuck — pod restart will recover."
        ),
    }


async def force_one_tick() -> dict:
    """Run a single _tick() out of band. Useful when the operator
    just unblocked a gate (lane toggle, ladder, seat rotation) and
    wants the queue drained NOW instead of waiting up to `interval_sec`.
    Safe to call concurrently with the scheduled loop — `_tick` is
    re-entrant against shared state."""
    global _TICK_COUNT, _LAST_TICK_TS, _LAST_TICK_RESULTS, _LAST_TICK_EXECUTED, _LAST_TICK_ERROR
    try:
        results = await _tick()
        _TICK_COUNT += 1
        _LAST_TICK_TS = _now_iso()
        _LAST_TICK_RESULTS = len(results) if results else 0
        _LAST_TICK_EXECUTED = sum(
            1 for r in (results or []) if r.get("verdict") == "executed"
        )
        _LAST_TICK_ERROR = None
        return {
            "ok": True,
            "ts": _LAST_TICK_TS,
            "results_count": _LAST_TICK_RESULTS,
            "executed_count": _LAST_TICK_EXECUTED,
            "results": results or [],
        }
    except Exception as e:  # noqa: BLE001
        _LAST_TICK_ERROR = f"{type(e).__name__}: {e}"
        return {"ok": False, "error": _LAST_TICK_ERROR}


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
