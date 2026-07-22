"""Stage functions for `auto_router._route_one` — 5 focused stages.

2026-02-11 (P6b-finish): the ~800-line `_route_one` body has been
split into five stage functions here, each accepting a
`RouteContext`. `_route_one` in `auto_router.py` is now a thin
orchestrator that calls these in sequence, short-circuiting on
the first stage that returns a verdict dict.

Contract for every stage:
    async def _gate_XXX(ctx: RouteContext) -> Optional[dict]

Return `None` → continue to the next stage.
Return a dict → short-circuit; that dict is the caller's verdict
(same shape the pre-refactor code returned inline).

Behavior guarantee: no observable change vs. the pre-P6b code path.
Every db.update_one / executions.record / learning.capture_experience
call site is preserved bit-for-bit; only the enclosing control flow
was flattened.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Optional

from namespaces import SHARED_INTENTS
from shared.auto_router_helpers import RouteContext, resolve_notional


logger = logging.getLogger("auto_router")


def _min_conviction_mult() -> float:
    """Floor for the combined seat×arbiter conviction multiplier.
    0 disables the floor (restores hard SIZED_TO_ZERO). Read at call
    time so operators can retune via env without a code change."""
    try:
        return max(0.0, min(1.0, float(
            os.environ.get("AUTO_ROUTER_MIN_CONVICTION_MULT", "0.25"),
        )))
    except (TypeError, ValueError):
        return 0.25


# ── Operator knob (2026-07-21): runtime_flags override ─────────────
# `runtime_flags._id=conviction_floor` beats the env default so the
# operator can retune from Operator Control without a redeploy. The
# Mongo value is cached ~15s; env fallback is NOT cached so tests
# (which monkeypatch the env) stay deterministic.
import time as _time  # noqa: E402

_FLOOR_CACHE: dict = {"val": None, "ts": 0.0}
_FLOOR_TTL_SEC = 15.0


def invalidate_conviction_floor_cache() -> None:
    _FLOOR_CACHE["val"] = None
    _FLOOR_CACHE["ts"] = 0.0


def peek_conviction_floor() -> float:
    """Sync best-effort read for status payloads: cached Mongo value
    if fresh, else the env default."""
    if (
        _FLOOR_CACHE["val"] is not None
        and (_time.monotonic() - _FLOOR_CACHE["ts"]) < _FLOOR_TTL_SEC
    ):
        return _FLOOR_CACHE["val"]
    return _min_conviction_mult()


async def get_conviction_floor() -> float:
    now = _time.monotonic()
    if (
        _FLOOR_CACHE["val"] is not None
        and (now - _FLOOR_CACHE["ts"]) < _FLOOR_TTL_SEC
    ):
        return _FLOOR_CACHE["val"]
    try:
        doc = await _db()["runtime_flags"].find_one(
            {"_id": "conviction_floor"}, {"value": 1},
        )
        if doc and doc.get("value") is not None:
            val = max(0.0, min(1.0, float(doc["value"])))
            _FLOOR_CACHE["val"] = val
            _FLOOR_CACHE["ts"] = now
            return val
    except Exception:  # noqa: BLE001
        pass
    return _min_conviction_mult()


def _db():
    """Late-bound db handle.

    The live_execution test-suite mocks `shared.auto_router.db` with
    `patch.object(ar, "db", fake_db)`. If we bound `db` at import
    time here (`from db import db`), those patches would bypass us
    and stage writes would hit the real Mongo. Reading via
    `shared.auto_router.db` at CALL time preserves the mock contract.
    """
    from shared import auto_router  # noqa: WPS433
    return auto_router.db


# Live-broker routes (must reserve capital against the ledger).
LIVE_ROUTES = {"live_micro", "live_normal"}
# Actions that OPEN a position → consume capital. Exits (SELL/COVER)
# release the entry's reservation and do NOT reserve themselves.
ENTRY_ACTIONS = {"BUY", "SHORT"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _auto_router_email() -> str:
    # Import lazily so the constants stay owned by auto_router.py.
    from shared.auto_router import AUTO_ROUTER_EMAIL  # noqa: WPS433
    return AUTO_ROUTER_EMAIL


# ─────────────────────────── Stage 1 ───────────────────────────
async def _gate_master_switch(ctx: RouteContext) -> Optional[dict]:
    """Operator arm-state preflight (2026-02-19 doctrine).

    Manual `/api/execution/submit` calls flow through here too, so
    we gate them on the arm state — otherwise the switch could be
    bypassed via the direct submit endpoint.
    """
    from shared.auto_router import _is_master_switch_armed  # noqa: WPS433

    if await _is_master_switch_armed():
        return None
    try:
        await _db()[SHARED_INTENTS].update_one(
            {"intent_id": ctx.intent_id},
            {"$set": {
                "gate_state": "blocked",
                "broker_reason": "master_switch_disarmed",
                "routed_at": _now_iso(),
            }},
        )
    except Exception:  # noqa: BLE001
        pass
    return {
        "verdict": "blocked",
        "reason": "master_switch_disarmed",
        "intent_id": ctx.intent_id,
    }


# ─────────────────────────── Stage 2 ───────────────────────────
async def _gate_seat(ctx: RouteContext) -> Optional[dict]:
    """Seat layer verdict + notional resolution.

    Notional MUST be resolved before seat.decide is skipped/failed —
    we persist `notional_source` on the intent as part of the block
    payload so the funnel shows what the router WOULD have shipped.
    """
    from shared import executions, seat  # noqa: WPS433

    ctx.notional_raw, ctx.notional_source = resolve_notional(ctx.intent)

    # ── 2026-07-22 opportunity policy: authority window + tiers ──
    # (aggression lives HERE, safety gates below stay untouched)
    from shared.opportunity.policy import (  # noqa: WPS433
        classify_tier, get_opportunity_policy,
    )
    policy = await get_opportunity_policy()
    lane = (ctx.intent.get("lane") or "").lower()

    # Execution authority: a signal older than the lane window is
    # RETAINED (72h sweeper) but never EXECUTED — the opportunity
    # has passed. Replaces the flat 120-min window for execution.
    authority_min = policy["authority_min"].get(lane)
    ingest_ts = ctx.intent.get("ingest_ts")
    if authority_min and ingest_ts:
        try:
            age_min = (
                datetime.now(timezone.utc)
                - datetime.fromisoformat(str(ingest_ts))
            ).total_seconds() / 60.0
        except ValueError:
            age_min = 0.0
        if age_min > float(authority_min):
            try:
                await _db()[SHARED_INTENTS].update_one(
                    {"intent_id": ctx.intent_id},
                    {"$set": {
                        "gate_state": "expired_unrouted",
                        "broker_reason": "AUTHORITY_EXPIRED",
                        "broker_error_detail": (
                            f"intent age {age_min:.1f}m > {lane} authority "
                            f"window {authority_min:.0f}m"
                        ),
                        "routed_at": _now_iso(),
                    }},
                )
            except Exception:  # noqa: BLE001
                pass
            return {"verdict": "blocked", "reason": "authority_expired",
                    "intent_id": ctx.intent_id}

    # Conviction → action tier: WATCH / PROBE / ENTER / FULL.
    action_upper = str(ctx.intent.get("action") or "").upper()
    if policy["tiers_enabled"] and action_upper in ("BUY", "SELL"):
        tier, tier_notional = classify_tier(
            float(ctx.intent.get("confidence") or 0.0), lane, policy,
        )
        if tier == "WATCH":
            try:
                await _db()[SHARED_INTENTS].update_one(
                    {"intent_id": ctx.intent_id},
                    {"$set": {
                        "gate_state": "blocked",
                        "action_tier": "WATCH",
                        "broker_reason": "BELOW_PROBE_THRESHOLD",
                        "routed_at": _now_iso(),
                    }},
                )
            except Exception:  # noqa: BLE001
                pass
            return {"verdict": "blocked", "reason": "below_probe_threshold",
                    "intent_id": ctx.intent_id}
        ctx.notional_raw = tier_notional
        ctx.notional_source = f"tier_{tier.lower()}"
        try:
            await _db()[SHARED_INTENTS].update_one(
                {"intent_id": ctx.intent_id},
                {"$set": {"action_tier": tier}},
            )
        except Exception:  # noqa: BLE001
            pass

    ctx.sd = await seat.decide(ctx.intent)
    if ctx.sd.verdict == "fire":
        return None

    sd = ctx.sd
    await executions.record(
        intent=ctx.intent,
        seat_verdict=sd.verdict,
        seat_holder=sd.executor,
        seat_reason=sd.reason,
        strategist=sd.strategist,
        governor=sd.governor,
        executor=sd.executor,
        auditor=sd.auditor,
        risk_multiplier=sd.risk_multiplier,
        risk_ok=False,
        risk_reason="seat_did_not_fire",
        notional_usd=ctx.notional_raw,
        ok=False,
    )
    # Doctrine 2026-07-12 (Step 7): NO SILENT RETURNS. Every
    # blocked/advisory intent MUST persist a `broker_reason`.
    terminal_state = (
        "advisory_only" if sd.verdict == "pass" else "blocked"
    )
    seat_reason_code = (
        "SEAT_ADVISORY_ONLY" if sd.verdict == "pass"
        else "SEAT_DID_NOT_FIRE"
    )
    try:
        await _db()[SHARED_INTENTS].update_one(
            {"intent_id": ctx.intent_id},
            {"$set": {
                "gate_state": terminal_state,
                "last_submit_ts": _now_iso(),
                "last_submit_by": _auto_router_email(),
                "seat_reason": sd.reason,
                "broker_reason": seat_reason_code,
                "broker_error_bucket": "seat",
                "broker_error_detail": str(sd.reason)[:500],
                "notional_source": ctx.notional_source,
            }},
        )
    except Exception:  # noqa: BLE001
        pass
    return {
        "verdict": "blocked",
        "reason": sd.reason,
        "seat_holder": sd.executor,
        "intent_brain": sd.intent_brain,
        "lane": sd.lane,
    }


# ─────────────────────────── Stage 3 ───────────────────────────
async def _gate_risk(ctx: RouteContext) -> Optional[dict]:
    """Governor multiplier → pair-floor → risk.check → market-hours
    preflight → capital-ledger reserve.

    Any block short-circuits with the ONE audit + intent-stamp that
    the pre-refactor code had inline. On success, sets
    `ctx.final_notional`, `ctx.rc`, and the ledger fields.
    """
    from shared import executions, risk  # noqa: WPS433

    sd = ctx.sd
    # 2a. Governor multiplier applied here — ONE PASS.
    adjusted_notional = max(0.0, ctx.notional_raw * sd.risk_multiplier)

    # 2a-ii. Soft degradation (Phase 2, 2026-07-20): the arbiter's
    # conviction multiplier (disagreement × quality, stamped on
    # evidence.size_multiplier at emit time) now scales size. Weak
    # conviction = smaller order, not a dead intent.
    arb_mult = 1.0
    try:
        raw_m = (ctx.intent.get("evidence") or {}).get("size_multiplier")
        if raw_m is not None:
            arb_mult = min(1.0, max(0.0, float(raw_m)))
    except (TypeError, ValueError):
        arb_mult = 1.0
    adjusted_notional *= arb_mult

    # 2a-ii-b. Conviction multiplier floor (2026-07-21, operator
    # directive): doctrine dampens trades, it does not kill them.
    # When seat×arbiter collapses the size below floor×base, trade
    # at floor×base instead of dying as SIZED_TO_ZERO.
    conviction_floor = await get_conviction_floor()
    conviction_floored = False
    if (
        conviction_floor > 0
        and ctx.notional_raw > 0
        and adjusted_notional < ctx.notional_raw * conviction_floor
    ):
        adjusted_notional = ctx.notional_raw * conviction_floor
        conviction_floored = True
        logger.info(
            "auto_router conviction floor ×%.2f applied intent=%s "
            "(seat=%.2f arb=%.2f) → $%.4f",
            conviction_floor, ctx.intent_id, sd.risk_multiplier,
            arb_mult, adjusted_notional,
        )
    final_notional = adjusted_notional

    # 2a-iii. Sized-to-zero is a conviction outcome, not a risk
    # rejection — stamp advisory_only so the kill map reads honestly.
    if final_notional <= 0:
        await executions.record(
            intent=ctx.intent,
            seat_verdict=sd.verdict,
            seat_holder=sd.executor,
            seat_reason=sd.reason,
            strategist=sd.strategist,
            governor=sd.governor,
            executor=sd.executor,
            auditor=sd.auditor,
            angels=sd.angels,
            risk_multiplier=sd.risk_multiplier,
            risk_ok=False,
            risk_reason="sized_to_zero",
            notional_usd=0.0,
            broker_status="sized_to_zero",
            ok=False,
        )
        try:
            await _db()[SHARED_INTENTS].update_one(
                {"intent_id": ctx.intent_id},
                {"$set": {
                    "gate_state": "advisory_only",
                    "last_submit_ts": _now_iso(),
                    "last_submit_by": _auto_router_email(),
                    "broker_reason": "SIZED_TO_ZERO",
                    "broker_error_bucket": "conviction_sizing",
                    "notional_source": ctx.notional_source,
                    "sizing_degradation": {
                        "base_usd": ctx.notional_raw,
                        "seat_multiplier": sd.risk_multiplier,
                        "arbiter_multiplier": arb_mult,
                        "final_usd": 0.0,
                    },
                }},
            )
        except Exception:  # noqa: BLE001
            pass
        return {"verdict": "advisory_only", "reason": "SIZED_TO_ZERO",
                "seat_multiplier": sd.risk_multiplier,
                "arbiter_multiplier": arb_mult}

    # 2b. Kraken per-pair notional floor (crypto only).
    if ctx.lane == "crypto":
        from shared.kraken_pair_floors import apply_floor  # noqa: WPS433
        far = await apply_floor(
            (ctx.intent.get("symbol") or "").upper(),
            adjusted_notional,
        )
        if not far.allowed:
            await executions.record(
                intent=ctx.intent,
                seat_verdict=sd.verdict,
                seat_holder=sd.executor,
                seat_reason=sd.reason,
                strategist=sd.strategist,
                governor=sd.governor,
                executor=sd.executor,
                auditor=sd.auditor,
                angels=sd.angels,
                risk_multiplier=sd.risk_multiplier,
                risk_ok=False,
                risk_reason="pair_floor_reject",
                notional_usd=adjusted_notional,
                broker_status="blocked_by_pair_floor",
                exception_type="PairFloorReject",
                exception_msg=(far.reject_reason or "")[:500],
                ok=False,
            )
            try:
                await _db()[SHARED_INTENTS].update_one(
                    {"intent_id": ctx.intent_id},
                    {"$set": {
                        "gate_state": "blocked",
                        "last_submit_ts": _now_iso(),
                        "last_submit_by": _auto_router_email(),
                        "broker_reason": "notional_below_pair_floor",
                        "broker_error_bucket": "min_order_notional",
                        "broker_error_detail": far.reject_reason,
                        "notional_source": ctx.notional_source,
                    }},
                )
            except Exception:  # noqa: BLE001
                pass
            return {"verdict": "blocked",
                    "reason": "notional_below_pair_floor",
                    "detail": far.reject_reason}
        final_notional = far.notional_usd
        if far.adjusted:
            logger.info(
                "auto_router pair-floor size_up $%.4f → $%.4f for %s",
                far.original_notional, far.notional_usd, far.floor.pair,
            )
            # Cap-authority guard (2026-02-28 doctrine).
            cap = risk.per_order_cap()
            if far.notional_usd > cap:
                detail = (
                    f"floor=${far.notional_usd:.4f}>cap=${cap:.4f} "
                    f"for {far.floor.pair}"
                )
                await executions.record(
                    intent=ctx.intent,
                    seat_verdict=sd.verdict,
                    seat_holder=sd.executor,
                    seat_reason=sd.reason,
                    strategist=sd.strategist,
                    governor=sd.governor,
                    executor=sd.executor,
                    auditor=sd.auditor,
                    angels=sd.angels,
                    risk_multiplier=sd.risk_multiplier,
                    risk_ok=False,
                    risk_reason=f"pair_floor_exceeds_per_order_cap:{detail}",
                    notional_usd=far.notional_usd,
                    broker_status="blocked_by_cap_authority",
                    exception_type="PairFloorExceedsCap",
                    exception_msg=detail,
                    ok=False,
                )
                try:
                    await _db()[SHARED_INTENTS].update_one(
                        {"intent_id": ctx.intent_id},
                        {"$set": {
                            "gate_state": "blocked",
                            "last_submit_ts": _now_iso(),
                            "last_submit_by": _auto_router_email(),
                            "broker_reason": "pair_floor_exceeds_per_order_cap",
                            "broker_error_bucket": "min_order_notional",
                            "broker_error_detail": detail,
                            "notional_source": ctx.notional_source,
                        }},
                    )
                except Exception:  # noqa: BLE001
                    pass
                return {"verdict": "blocked",
                        "reason": "pair_floor_exceeds_per_order_cap",
                        "floor_usd": far.notional_usd,
                        "cap_usd": cap,
                        "pair": far.floor.pair}

    # 2c. Equity broker floor size-up (2026-07-20 soft degradation).
    # Webull rejects orders under $5 (operator-confirmed broker
    # change). Mirrors the crypto pair-floor doctrine: size UP to the
    # floor instead of letting a quality-dampened order die at
    # WEBULL_NOTIONAL_BELOW_FLOOR. Cap stays the authority — a floor
    # above the per-order cap blocks honestly.
    floor_sized_up = False
    equity_floor_usd = None
    if ctx.lane == "equity":
        from shared.broker.webull_caps import webull_notional_band  # noqa: WPS433
        eq_lo, _eq_hi, _src = webull_notional_band(None)
        equity_floor_usd = eq_lo
        if final_notional < eq_lo:
            cap = risk.per_order_cap()
            if eq_lo > cap:
                detail = (
                    f"floor=${eq_lo:.2f}>cap=${cap:.2f} "
                    f"for {ctx.intent.get('symbol')}"
                )
                await executions.record(
                    intent=ctx.intent,
                    seat_verdict=sd.verdict,
                    seat_holder=sd.executor,
                    seat_reason=sd.reason,
                    strategist=sd.strategist,
                    governor=sd.governor,
                    executor=sd.executor,
                    auditor=sd.auditor,
                    angels=sd.angels,
                    risk_multiplier=sd.risk_multiplier,
                    risk_ok=False,
                    risk_reason=f"equity_floor_exceeds_per_order_cap:{detail}",
                    notional_usd=final_notional,
                    broker_status="blocked_by_cap_authority",
                    exception_type="EquityFloorExceedsCap",
                    exception_msg=detail,
                    ok=False,
                )
                try:
                    await _db()[SHARED_INTENTS].update_one(
                        {"intent_id": ctx.intent_id},
                        {"$set": {
                            "gate_state": "blocked",
                            "last_submit_ts": _now_iso(),
                            "last_submit_by": _auto_router_email(),
                            "broker_reason": "equity_floor_exceeds_per_order_cap",
                            "broker_error_bucket": "min_order_notional",
                            "broker_error_detail": detail,
                            "notional_source": ctx.notional_source,
                        }},
                    )
                except Exception:  # noqa: BLE001
                    pass
                return {"verdict": "blocked",
                        "reason": "equity_floor_exceeds_per_order_cap",
                        "floor_usd": eq_lo, "cap_usd": cap}
            logger.info(
                "auto_router equity floor size_up $%.2f → $%.2f for %s",
                final_notional, eq_lo, ctx.intent.get("symbol"),
            )
            final_notional = eq_lo
            floor_sized_up = True

    # 2d. Sizing provenance — full trail of how the size was derived.
    try:
        await _db()[SHARED_INTENTS].update_one(
            {"intent_id": ctx.intent_id},
            {"$set": {"sizing_degradation": {
                "base_usd": ctx.notional_raw,
                "notional_source": ctx.notional_source,
                "seat_multiplier": sd.risk_multiplier,
                "arbiter_multiplier": arb_mult,
                "conviction_floor_applied": conviction_floored,
                "conviction_floor_mult": conviction_floor,
                "floor_sized_up": floor_sized_up,
                "equity_floor_usd": equity_floor_usd,
                "final_usd": final_notional,
                "ts": _now_iso(),
            }}},
        )
    except Exception:  # noqa: BLE001
        pass

    # 3. Risk hard limits.
    rc = await risk.check(ctx.intent, notional_usd=final_notional)
    ctx.rc = rc
    if not rc.ok:
        await executions.record(
            intent=ctx.intent,
            seat_verdict=sd.verdict,
            seat_holder=sd.executor,
            seat_reason=sd.reason,
            strategist=sd.strategist,
            governor=sd.governor,
            executor=sd.executor,
            auditor=sd.auditor,
            angels=sd.angels,
            risk_multiplier=sd.risk_multiplier,
            risk_ok=False,
            risk_reason=rc.reason,
            notional_usd=rc.notional_usd,
            ok=False,
        )
        try:
            await _db()[SHARED_INTENTS].update_one(
                {"intent_id": ctx.intent_id},
                {"$set": {
                    "gate_state": "blocked",
                    "last_submit_ts": _now_iso(),
                    "last_submit_by": _auto_router_email(),
                    "risk_reason": rc.reason,
                    "broker_reason": "RISK_REJECTED",
                    "broker_error_bucket": "risk",
                    "broker_error_detail": str(rc.reason)[:500],
                    "notional_source": ctx.notional_source,
                }},
            )
        except Exception:  # noqa: BLE001
            pass
        return {"verdict": "blocked", "reason": rc.reason}

    # Adopt risk's authoritative notional (equity per-order cap clip).
    final_notional = rc.notional_usd

    # 3a. Equity market-closed pre-flight (2026-07-06).
    if ctx.lane == "equity":
        from shared.market_hours import (  # noqa: WPS433
            is_equity_extended_hours,
            is_equity_rth,
            market_hours_reason,
        )
        from routes.equity_extended_hours_admin import (  # noqa: WPS433
            get_equity_extended_hours_enabled,
        )
        ext_hours_on = await get_equity_extended_hours_enabled()
        market_open = (
            is_equity_extended_hours() if ext_hours_on else is_equity_rth()
        )
        if not market_open:
            reason = market_hours_reason()
            await executions.record(
                intent=ctx.intent,
                seat_verdict=sd.verdict,
                seat_holder=sd.executor,
                seat_reason=sd.reason,
                strategist=sd.strategist,
                governor=sd.governor,
                executor=sd.executor,
                auditor=sd.auditor,
                angels=sd.angels,
                risk_multiplier=sd.risk_multiplier,
                risk_ok=rc.ok,
                risk_reason=rc.reason,
                notional_usd=final_notional,
                broker_status="market_closed_preflight",
                ok=False,
            )
            try:
                await _db()[SHARED_INTENTS].update_one(
                    {"intent_id": ctx.intent_id},
                    {"$set": {
                        "gate_state": "blocked",
                        "last_submit_ts": _now_iso(),
                        "last_submit_by": _auto_router_email(),
                        "broker_reason": "market_closed_preflight",
                        "broker_error_bucket": "market_closed",
                        "broker_error_detail": reason[:500],
                        "notional_source": ctx.notional_source,
                    }},
                )
            except Exception:  # noqa: BLE001
                pass
            return {
                "verdict": "blocked",
                "reason": "market_closed_preflight",
                "detail": reason,
                "extended_hours_enabled": ext_hours_on,
            }

    # 3b. Ladder-aware sizing + capital-ledger reserve.
    try:
        from shared.sizing_gate import evaluate_sizing_with_ladder  # noqa: WPS433
        sizing = await evaluate_sizing_with_ladder(
            requested_usd=final_notional,
            brain=(ctx.intent.get("stack") or ctx.intent.get("stack_canonical") or ""),
            lane=(ctx.intent.get("lane") or None),
        )
        action = ctx.action_upper
        route_is_live = sizing.route in LIVE_ROUTES

        if route_is_live:
            try:
                await _db()[SHARED_INTENTS].update_one(
                    {"intent_id": ctx.intent_id},
                    {"$set": {
                        "sizing_provenance": {
                            "route": sizing.route,
                            "stage": sizing.stage,
                            "binding_rail": sizing.binding_rail,
                            "final_usd": final_notional,
                            "ladder_cap_usd": sizing.ladder_cap_usd,
                            "execution_mode": sizing.execution_mode,
                        },
                    }},
                )
            except Exception:  # noqa: BLE001
                pass

        # Ledger reserve — live routes AND entry actions only.
        if (
            route_is_live
            and action in ENTRY_ACTIONS
            and ctx.ledger_lane in ("equity", "crypto")
            and final_notional > 0
        ):
            from shared.capital.ledger import (  # noqa: WPS433
                get_lane_headroom, reserve_capital,
            )
            head = await get_lane_headroom(ctx.ledger_lane)
            if head is None:
                logger.debug(
                    "auto_router capital_ledger SKIP — lane=%s "
                    "not initialised", ctx.ledger_lane,
                )
            else:
                ctx.ledger_reserve_amount = final_notional
                ok = await reserve_capital(
                    lane=ctx.ledger_lane,
                    amount=ctx.ledger_reserve_amount,
                    intent_id=ctx.intent_id,
                )
                if not ok:
                    logger.warning(
                        "auto_router capital_ledger REJECTED "
                        "intent=%s lane=%s amount=%.2f — cap exceeded",
                        ctx.intent_id, ctx.ledger_lane,
                        ctx.ledger_reserve_amount,
                    )
                    await executions.record(
                        intent=ctx.intent,
                        seat_verdict=sd.verdict,
                        seat_holder=sd.executor,
                        seat_reason=sd.reason,
                        strategist=sd.strategist,
                        governor=sd.governor,
                        executor=sd.executor,
                        auditor=sd.auditor,
                        angels=sd.angels,
                        risk_multiplier=sd.risk_multiplier,
                        risk_ok=rc.ok,
                        risk_reason=rc.reason,
                        notional_usd=final_notional,
                        broker_status="blocked_by_capital_ledger",
                        exception_type="CapitalLedgerRejected",
                        exception_msg=(
                            f"cap exceeded lane={ctx.ledger_lane} "
                            f"requested={ctx.ledger_reserve_amount:.2f}"
                        ),
                        ok=False,
                    )
                    try:
                        await _db()[SHARED_INTENTS].update_one(
                            {"intent_id": ctx.intent_id},
                            {"$set": {
                                "gate_state": "blocked",
                                "last_submit_ts": _now_iso(),
                                "last_submit_by": _auto_router_email(),
                                "broker_reason": "REJECTED_CAP_EXCEEDED",
                                "broker_error_bucket": "capital_ledger_cap",
                                "notional_source": ctx.notional_source,
                            }},
                        )
                    except Exception:  # noqa: BLE001
                        pass
                    return {
                        "verdict": "blocked",
                        "reason": "REJECTED_CAP_EXCEEDED",
                        "lane": ctx.ledger_lane,
                        "requested_notional": ctx.ledger_reserve_amount,
                    }
                ctx.ledger_reserved = True
    except Exception as exc:  # noqa: BLE001
        # Sizing / ledger integration is defensive — never let a
        # module-import / db issue block the broker path.
        logger.debug(
            "auto_router sizing/ledger gate skipped intent=%s: %r",
            ctx.intent_id, exc,
        )

    ctx.final_notional = final_notional
    return None


# ─────────────────────────── Stage 4 ───────────────────────────
async def _route_and_submit(ctx: RouteContext) -> Optional[dict]:
    """Broker call + broker-error taxonomy handling.

    On success stores the order dict on `ctx.order` and returns None
    so Stage 5 can finalise. On terminal/transient failure returns
    the verdict dict (identical shape to the pre-refactor path).
    """
    from shared import executions  # noqa: WPS433
    from shared.auto_router import AUTO_ROUTER_MAX_BROKER_RETRIES  # noqa: WPS433
    from shared.broker_router import (  # noqa: WPS433
        BrokerRouteBlocked, route_order,
    )

    sd = ctx.sd
    rc = ctx.rc
    try:
        order = await route_order(
            ctx.intent,
            notional_usd=ctx.final_notional,
            client_order_id=f"ar-{ctx.intent_id[:24]}",
        )
    except BrokerRouteBlocked as exc:
        await executions.record(
            intent=ctx.intent,
            seat_verdict=sd.verdict,
            seat_holder=sd.executor,
            seat_reason=sd.reason,
            strategist=sd.strategist,
            governor=sd.governor,
            executor=sd.executor,
            auditor=sd.auditor,
            risk_multiplier=sd.risk_multiplier,
            risk_ok=rc.ok,
            risk_reason=rc.reason,
            notional_usd=rc.notional_usd,
            broker_status="blocked_by_broker_router",
            exception_type="BrokerRouteBlocked",
            exception_msg=str(exc)[:500],
            ok=False,
        )
        try:
            await _db()[SHARED_INTENTS].update_one(
                {"intent_id": ctx.intent_id},
                {"$set": {
                    "gate_state": "blocked",
                    "last_submit_ts": _now_iso(),
                    "last_submit_by": _auto_router_email(),
                    "broker_reason": str(exc)[:500],
                    "notional_source": ctx.notional_source,
                }},
            )
        except Exception:  # noqa: BLE001
            pass
        return {"verdict": "blocked", "reason": str(exc)}
    except Exception as exc:  # noqa: BLE001
        exc_type = type(exc).__name__
        exc_msg = str(exc)[:1000]

        # Broker-error taxonomy (2026-02-17 doctrine).
        from shared.broker_error_taxonomy import classify  # noqa: WPS433
        err = classify(exc)
        retry_count_before = int(ctx.intent.get("broker_retry_count") or 0)

        logger.error(
            "auto_router broker call raised intent=%s symbol=%s action=%s "
            "exc=%s bucket=%s terminal=%s retry_count=%d msg=%s",
            ctx.intent_id, ctx.intent.get("symbol"),
            ctx.intent.get("action"),
            exc_type, err.bucket, err.is_terminal,
            retry_count_before, exc_msg,
        )
        await executions.record(
            intent=ctx.intent,
            seat_verdict=sd.verdict,
            seat_holder=sd.executor,
            seat_reason=sd.reason,
            strategist=sd.strategist,
            governor=sd.governor,
            executor=sd.executor,
            auditor=sd.auditor,
            angels=sd.angels,
            risk_multiplier=sd.risk_multiplier,
            risk_ok=rc.ok,
            risk_reason=rc.reason,
            notional_usd=rc.notional_usd,
            exception_type=exc_type,
            exception_msg=exc_msg,
            broker_status=f"broker_error:{err.bucket}",
            ok=False,
        )

        should_terminate = err.is_terminal
        terminal_reason = err.bucket
        if not err.is_terminal:
            new_retry_count = retry_count_before + 1
            if new_retry_count >= AUTO_ROUTER_MAX_BROKER_RETRIES:
                should_terminate = True
                terminal_reason = "broker_retry_exhausted"

        if should_terminate:
            # Release ledger reservation (if held).
            if ctx.ledger_reserved:
                try:
                    from shared.capital.ledger import release_capital  # noqa: WPS433
                    await release_capital(
                        lane=ctx.ledger_lane,
                        intent_id=ctx.intent_id,
                        amount=ctx.ledger_reserve_amount,
                        reason="broker_terminal_reject",
                    )
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "auto_router: release_capital failed on "
                        "broker terminal intent=%s", ctx.intent_id,
                    )
            try:
                await _db()[SHARED_INTENTS].update_one(
                    {"intent_id": ctx.intent_id},
                    {"$set": {
                        "gate_state": "blocked",
                        "last_submit_ts": _now_iso(),
                        "last_submit_by": _auto_router_email(),
                        "broker_reason": terminal_reason,
                        "broker_error_detail": err.detail,
                        "broker_error_bucket": err.bucket,
                        "notional_source": ctx.notional_source,
                    }},
                )
            except Exception:  # noqa: BLE001
                pass

            # 2026-07-09 live-learning capture (Stage 1).
            try:
                from shared.learning.live_loop import capture_experience  # noqa: WPS433
                learn_intent = dict(ctx.intent)
                learn_intent.setdefault("execution", {})
                learn_intent["execution"]["action"] = ctx.action_upper
                learn_intent["execution"]["notional_usd"] = ctx.final_notional
                learn_intent["final_notional_usd"] = ctx.final_notional
                learn_intent["notional_source"] = ctx.notional_source
                await capture_experience(
                    _db(),
                    intent=learn_intent,
                    broker_receipt={
                        "status": "rejected",
                        "broker": (
                            err.detail.get("broker")
                            if hasattr(err, "detail") and isinstance(err.detail, dict)
                            else None
                        ),
                        "error_bucket": err.bucket,
                        "error_detail": err.detail if hasattr(err, "detail") else None,
                    },
                    terminal_state="broker_rejected",
                    reject_reason=terminal_reason,
                )
            except Exception as _learn_exc:  # noqa: BLE001
                logger.warning(
                    "learning.capture_experience (reject path) failed: %s",
                    _learn_exc,
                )
            return {
                "verdict": "blocked",
                "reason": terminal_reason,
                "broker_error_bucket": err.bucket,
                "exception_type": exc_type,
            }

        # Transient path — bump retry counter, leave intent eligible.
        try:
            await _db()[SHARED_INTENTS].update_one(
                {"intent_id": ctx.intent_id},
                {"$set": {
                    "last_submit_ts": _now_iso(),
                    "last_submit_by": _auto_router_email(),
                    "broker_error_bucket": err.bucket,
                    "broker_error_detail": err.detail,
                    "notional_source": ctx.notional_source,
                },
                 "$inc": {"broker_retry_count": 1}},
            )
        except Exception:  # noqa: BLE001
            pass
        return {
            "verdict": "error",
            "reason": exc_msg,
            "exception_type": exc_type,
            "broker_error_bucket": err.bucket,
            "broker_retry_count": retry_count_before + 1,
        }

    ctx.order = order
    return None


# ─────────────────────────── Stage 5 ───────────────────────────
async def _finalize_gate_state(ctx: RouteContext) -> dict:
    """Success path — write the submitted-state stamp, executions
    receipt, and live-learning training row. Returns the executed
    verdict dict."""
    from shared import executions  # noqa: WPS433

    sd = ctx.sd
    rc = ctx.rc
    order = ctx.order
    shipped_notional = ctx.final_notional

    await _db()[SHARED_INTENTS].update_one(
        {"intent_id": ctx.intent_id},
        {"$set": {
            "executed": True,
            "executed_at": _now_iso(),
            "executed_by": _auto_router_email(),
            "gate_state": "submitted",
            "final_notional_usd": shipped_notional,
            "notional_source": ctx.notional_source,
            "notional_usd": ctx.notional_raw,
            "broker_order": {
                k: order.get(k) for k in (
                    "id", "order_id", "broker", "broker_symbol", "canonical",
                    "lane", "side", "qty", "notional", "status",
                    "filled_qty", "filled_avg_price", "submitted_at",
                ) if order.get(k) is not None
            },
        }},
    )
    await executions.record(
        intent=ctx.intent,
        seat_verdict=sd.verdict,
        seat_holder=sd.executor,
        seat_reason=sd.reason,
        strategist=sd.strategist,
        governor=sd.governor,
        executor=sd.executor,
        auditor=sd.auditor,
        angels=sd.angels,
        risk_multiplier=sd.risk_multiplier,
        risk_ok=rc.ok,
        risk_reason=rc.reason,
        notional_usd=shipped_notional,
        broker=order.get("broker"),
        broker_order_id=order.get("id") or order.get("order_id"),
        broker_status=order.get("status") or "submitted",
        broker_response=order,
        ok=True,
    )

    # 2026-07-09 live-learning capture (Stage 1).
    try:
        from shared.learning.live_loop import capture_experience  # noqa: WPS433
        learning_intent = dict(ctx.intent)
        learning_intent.setdefault("execution", {})
        learning_intent["execution"]["action"] = ctx.action_upper
        learning_intent["execution"]["notional_usd"] = shipped_notional
        learning_intent["final_notional_usd"] = shipped_notional
        learning_intent["notional_source"] = ctx.notional_source
        learning_intent["broker_order"] = {
            k: order.get(k) for k in (
                "id", "order_id", "broker", "status",
                "filled_qty", "filled_avg_price",
            ) if order.get(k) is not None
        }
        await capture_experience(
            _db(),
            intent=learning_intent,
            broker_receipt=order,
            terminal_state="submitted",
        )
    except Exception as _learn_exc:  # noqa: BLE001
        logger.warning(
            "learning.capture_experience (success path) failed: %s",
            _learn_exc,
        )
    logger.info(
        "auto_router OK intent=%s symbol=%s action=%s notional=%.2f "
        "broker=%s order_id=%s",
        ctx.intent_id, ctx.intent.get("symbol"), ctx.intent.get("action"),
        shipped_notional, order.get("broker"),
        order.get("id") or order.get("order_id"),
    )
    return {
        "verdict": "executed",
        "intent_id": ctx.intent_id,
        "final_notional": shipped_notional,
        "notional_usd": shipped_notional,
        "broker": order.get("broker"),
        "order_id": order.get("id") or order.get("order_id"),
    }
