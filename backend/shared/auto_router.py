"""Auto-router — Unified Pipeline edition.

Periodically scans `shared_intents` for unexecuted, routable intents
and delegates the decision to `shared.pipeline.execution_pipeline` —
the single source of authority. Three hard blockers: Seat, RoadGuard,
Broker. One receipt per intent written to `pipeline_receipts`.

Refactored 2026-06-18: the legacy 20-gate chain (Phase 0 classifier →
ladder → sizing → kill-switch → 20-gate → in-flight dedupe → broker →
side-effects → receipt) was deleted now that the Unified Pipeline has
been load-bearing in Prod since 2026-06-17. The operator kill switch
that previously lived inside the legacy chain has been ported into
RoadGuard so it remains a first-class hard stop.

Doctrine still in force:
  * Per-intent idempotency via `executed=true` on `shared_intents`.
  * Per-tick rate cap (AUTO_ROUTER_MAX_PER_TICK) — protects broker
    quotas + gives the operator a chance to see/intervene on bursts.
  * Per-lane seat-occupancy filter: an intent only runs if at least
    one brain currently holds the executor seat for its lane.
  * `_sweep_seat_mismatched_intents` drains legacy limbo (intents
    posted while a different brain held the seat).
  * Attribution to a synthetic operator email so pipeline receipts
    can be distinguished from operator-clicked fills.

Disable with: AUTO_ROUTER_ENABLED=false in backend/.env, OR by
flipping `runtime_flags.auto_router_enabled.enabled=false` via
`POST /api/admin/auto-router/stop` (no redeploy).
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from db import db
from namespaces import SHARED_GATE_RESULTS, SHARED_INTENTS


logger = logging.getLogger("auto_router")

# ── Capital ledger integration (2026-02-20) ────────────────────────
# Live-route entry intents reserve against the per-lane capital cap
# ledger BEFORE broker submit; terminal broker rejects release the
# reservation. Doctrine + module: `shared/capital/ledger.py`.
LIVE_ROUTES = {"live_micro", "live_normal"}
# Action codes that OPEN a new position (reserve on submit). Exit
# actions (SELL / COVER) release the entry's reservation on position
# close and do NOT reserve themselves — a SELL is releasing capital,
# not consuming it. `SHORT` opens a short position and consumes cap.
ENTRY_ACTIONS = {"BUY", "SHORT"}

# Loop tunables — env-driven so we can poke them without redeploys.
AUTO_ROUTER_ENABLED = os.environ.get("AUTO_ROUTER_ENABLED", "true").lower() == "true"
AUTO_ROUTER_INTERVAL_SEC = int(os.environ.get("AUTO_ROUTER_INTERVAL_SEC", "30"))
# Default notional per auto-routed intent. Each intent can override
# via `intent.requested_notional_usd`; the pipeline's Seat layer caps
# this further per (brain × lane) policy.
AUTO_ROUTER_NOTIONAL_USD = float(os.environ.get("AUTO_ROUTER_NOTIONAL_USD", "10"))

# Per-tick rate cap. NOT obsolete and NOT redundant with the
# pipeline's duplicate-order check — they solve different problems:
#
#   AUTO_ROUTER_MAX_PER_TICK = rate cap (broker quota + operator
#       visibility on bursts). At 30s ticks × 5/tick that's a
#       sustained ceiling of ~10 orders/min.
#
#   Pipeline's duplicate_order (RoadGuard) = same-symbol dedupe.
#       Blocks the SAME (brain, lane, symbol, side) twice while one
#       is in flight; doesn't bound the burst rate across DIFFERENT
#       symbols.
#
# Coverage note (2026-07-06): the previous
# `tests/test_auto_router_max_per_tick.py` was deleted in a prior
# cleanup. This contract is currently NOT under direct pytest
# coverage — the `.to_list(AUTO_ROUTER_MAX_PER_TICK)` call in `_tick`
# is the only enforcement point. Re-add a small regression test if
# this bound ever needs to change or a race condition is suspected.
AUTO_ROUTER_MAX_PER_TICK = int(os.environ.get("AUTO_ROUTER_MAX_PER_TICK", "5"))
# Broker-retry ceiling for the truly-transient error class. Beyond
# this the intent is terminally stamped `gate_state=blocked` with
# `broker_reason=broker_retry_exhausted` so the tick queue drains
# instead of looping the same failing intent forever. Doctrine
# (2026-02-17): no intent retries indefinitely. Deterministic errors
# (market_closed, insufficient_funds, min_order_notional, etc.) go
# terminal on the first attempt; transient errors get this many
# retries before being terminated.
AUTO_ROUTER_MAX_BROKER_RETRIES = int(
    os.environ.get("AUTO_ROUTER_MAX_BROKER_RETRIES", "5")
)
# ── Expiration sweeper (2026-02-28) ──────────────────────────────
# `_tick` only samples intents within `AUTO_ROUTER_LOOKBACK_MIN`. Any
# transient-error intent that aged past the lookback silently vanished
# from the funnel because it was never terminally stamped. This env
# controls how old (in minutes) an unrouted intent can be before the
# sweeper stamps it `gate_state=expired_unrouted`. Default 120min —
# double the lookback so a legit late-arriving intent isn't cut off
# by racing the two windows.
AUTO_ROUTER_EXPIRE_MIN = int(
    os.environ.get("AUTO_ROUTER_EXPIRE_MIN", "120")
)
AUTO_ROUTER_EMAIL = "auto-router@mission-control"

# ── Master-switch preflight cache (2026-02-19) ──────────────────
# The operator's arm gate (`trading_controls.enabled` in Mongo) is
# now consulted before every tick AND every manual route. Prior to
# this the switch was UI-only; the loop respected `AUTO_ROUTER_ENABLED`
# env at boot and ignored the runtime doc, so `POST /api/admin/trading/toggle`
# was a placebo. Reading Mongo on every intent would be wasteful, so
# we cache the answer for a short TTL. The TTL is short enough
# (2s) that an operator disarm takes effect within one tick.
import time as _time_module  # noqa: E402
_ARM_CACHE_VAL: Optional[bool] = None
_ARM_CACHE_TS: float = 0.0
_ARM_CACHE_TTL_SEC = 2.0
_ARM_LAST_LOGGED: Optional[bool] = None


async def _is_master_switch_armed() -> bool:
    """Consult the operator's master-switch Mongo doc, cached ~2s.

    Fail-CLOSED on any error: an unreadable arm state means we do
    NOT submit new orders. The reconcile sweep still runs (called
    unconditionally at the top of `_tick`) so in-flight orders keep
    their acks flowing.
    """
    global _ARM_CACHE_VAL, _ARM_CACHE_TS, _ARM_LAST_LOGGED
    now = _time_module.monotonic()
    if _ARM_CACHE_VAL is not None and (now - _ARM_CACHE_TS) < _ARM_CACHE_TTL_SEC:
        return _ARM_CACHE_VAL
    try:
        from routes.trading_controls import is_trading_enabled  # noqa: WPS433
        armed = bool(await is_trading_enabled())
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "auto_router: master-switch read FAILED (%s: %s) — "
            "failing closed (armed=False)",
            type(exc).__name__, exc,
        )
        armed = False
    _ARM_CACHE_VAL = armed
    _ARM_CACHE_TS = now
    # State-change logging so the operator can grep the log for
    # exactly when the switch flipped.
    if _ARM_LAST_LOGGED is None or _ARM_LAST_LOGGED != armed:
        logger.warning(
            "auto_router: master-switch state = %s "
            "(gates all new intent submission)",
            "ARMED" if armed else "DISARMED",
        )
        _ARM_LAST_LOGGED = armed
    return armed


def _invalidate_arm_cache() -> None:
    """Force the next `_is_master_switch_armed` call to hit Mongo.
    Exposed for the toggle endpoint so operator flips take effect
    immediately instead of waiting for the TTL to expire."""
    global _ARM_CACHE_VAL, _ARM_CACHE_TS
    _ARM_CACHE_VAL = None
    _ARM_CACHE_TS = 0.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _route_one(intent: dict) -> dict:
    """The new path (2026-02-27 architectural reduction).

        Brain (already emitted) → Seat → Risk → Broker → Executions audit

    No dry-run. No auto-submit policy. No council. No consensus pool.
    No legacy brain wrappers. No unified pipeline. One row in the
    `executions` collection per attempt, period.

    Returns a verdict dict in the legacy shape so existing callers
    (status endpoint, post-mortem aggregator) keep working unchanged.

    2026-02-19: MASTER SWITCH PREFLIGHT. Manual `/api/execution/submit`
    calls flow through here too, so we also gate them on the arm
    state — otherwise the switch could be bypassed via the direct
    submit endpoint.
    """
    from shared import executions, risk, seat  # noqa: WPS433

    intent_id = intent.get("intent_id") or ""

    # ── Master-switch preflight ─────────────────────────────────
    if not await _is_master_switch_armed():
        # Stamp the intent as blocked so the funnel is honest, then
        # short-circuit. No broker call is made.
        try:
            await db[SHARED_INTENTS].update_one(
                {"intent_id": intent_id},
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
            "intent_id": intent_id,
        }
    # ── Notional resolution ──────────────────────────────────────
    # Legacy path stamps `requested_notional_usd`; v3 envelope stamps
    # `execution.notional_usd`. Try both, in that order. If the brain
    # emitted a directional (BUY/SELL) intent with NO notional on either
    # slot, apply the micro-live default so the pipeline can send a
    # $5 probe order. Doctrine (2026-07-09 operator directive):
    #
    #   "direction exists now. The next executable choke is
    #    notional_usd=null … Add a micro-notional fallback."
    # ── Notional resolution (2026-07-09 operator directive) ──
    # 2026-07-12 (P6b): rules extracted to `auto_router_helpers.py`.
    # Full doctrine + all 4 branches documented there. Behavior
    # identical to the previous inline block.
    from shared.auto_router_helpers import resolve_notional
    action_upper = str(intent.get("action") or "").upper()
    notional_raw, notional_source = resolve_notional(intent)

    # ── 1. Seat decides ──────────────────────────────────────────
    sd = await seat.decide(intent)
    if sd.verdict != "fire":
        await executions.record(
            intent=intent,
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
            notional_usd=notional_raw,
            ok=False,
        )
        # Stamp the intent so the next tick skips it.
        # Doctrine 2026-07-12 (Step 7): NO SILENT RETURNS. Every
        # blocked/advisory intent MUST persist a `broker_reason` so
        # the funnel is honest at a single-field level. Previously
        # this branch only set `seat_reason`, producing 18,155 rows
        # with `broker_reason=None` — silent to the operator view.
        terminal_state = (
            "advisory_only" if sd.verdict == "pass" else "blocked"
        )
        _seat_reason_code = (
            "SEAT_ADVISORY_ONLY" if sd.verdict == "pass"
            else "SEAT_DID_NOT_FIRE"
        )
        try:
            await db[SHARED_INTENTS].update_one(
                {"intent_id": intent_id},
                {"$set": {
                    "gate_state": terminal_state,
                    "last_submit_ts": _now_iso(),
                    "last_submit_by": AUTO_ROUTER_EMAIL,
                    "seat_reason": sd.reason,
                    "broker_reason": _seat_reason_code,
                    "broker_error_bucket": "seat",
                    "broker_error_detail": str(sd.reason)[:500],
                    "notional_source": notional_source,
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

    # ── 2. Governor multiplier applied here — ONE PASS ──────────
    # Doctrine reorder (2026-02-28): the governor's risk_multiplier
    # is applied to the intent's requested notional BEFORE anything
    # downstream sees it. The result (`adjusted_notional`) is the
    # authoritative starting point for both pair-floor and risk.
    adjusted_notional = max(0.0, notional_raw * sd.risk_multiplier)
    final_notional = adjusted_notional

    # ── 2a. Kraken per-pair notional floor (crypto only) ────────
    # Doctrine reorder (2026-02-28): pair-floor NOW runs BEFORE risk
    # so `risk.check` sees the actual notional we intend to send to
    # the broker. Previously floor ran AFTER risk, which allowed the
    # per-order cap to be silently bypassed for crypto: risk approves
    # $5 → floor sizes up to $15 → broker gets $15 (past cap). With
    # this order, the cap-authority guard below catches the conflict
    # and blocks honestly.
    if (intent.get("lane") or "").lower() == "crypto":
        from shared.kraken_pair_floors import apply_floor  # noqa: WPS433
        far = await apply_floor(
            (intent.get("symbol") or "").upper(),
            adjusted_notional,
        )
        if not far.allowed:
            # Operator chose `policy=reject` for this pair. Terminate —
            # BUT audit the attempt first. Every pass through this
            # function writes exactly ONE execution row (doctrine); the
            # pre-fix code skipped the audit here, breaking the funnel
            # denominator and the "one row per attempt" contract.
            await executions.record(
                intent=intent,
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
                await db[SHARED_INTENTS].update_one(
                    {"intent_id": intent_id},
                    {"$set": {
                        "gate_state": "blocked",
                        "last_submit_ts": _now_iso(),
                        "last_submit_by": AUTO_ROUTER_EMAIL,
                        "broker_reason": "notional_below_pair_floor",
                        "broker_error_bucket": "min_order_notional",
                        "broker_error_detail": far.reject_reason,
                        "notional_source": notional_source,
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
            # ── Cap-authority guard (2026-02-28 doctrine) ──────────
            # Cap is authority. Floor is exchange constraint. If the
            # floor exceeds the operator-set per-order cap, block
            # honestly with `pair_floor_exceeds_per_order_cap` so the
            # operator can either raise the cap or set the pair's
            # policy=reject / disable trading on it. Silently letting
            # risk clip the floor would just recreate the original
            # Kraken volume-minimum-not-met rejection loop.
            cap = risk.per_order_cap()
            if far.notional_usd > cap:
                detail = (
                    f"floor=${far.notional_usd:.4f}>cap=${cap:.4f} "
                    f"for {far.floor.pair}"
                )
                await executions.record(
                    intent=intent,
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
                    await db[SHARED_INTENTS].update_one(
                        {"intent_id": intent_id},
                        {"$set": {
                            "gate_state": "blocked",
                            "last_submit_ts": _now_iso(),
                            "last_submit_by": AUTO_ROUTER_EMAIL,
                            "broker_reason": "pair_floor_exceeds_per_order_cap",
                            "broker_error_bucket": "min_order_notional",
                            "broker_error_detail": detail,
                            "notional_source": notional_source,
                        }},
                    )
                except Exception:  # noqa: BLE001
                    pass
                return {"verdict": "blocked",
                        "reason": "pair_floor_exceeds_per_order_cap",
                        "floor_usd": far.notional_usd,
                        "cap_usd": cap,
                        "pair": far.floor.pair}

    # ── 3. Risk hard limits ──────────────────────────────────────
    # Now risk sees the TRUE final notional going to the broker.
    # For crypto, the cap-authority guard above already ensured
    # `final_notional <= per_order_cap`, so risk's internal
    # `min(n, per_order)` is a no-op on that lane. For equity,
    # risk still silently clips at the cap — which is correct
    # behavior for the equity lane (no external floor to conflict).
    rc = await risk.check(intent, notional_usd=final_notional)
    if not rc.ok:
        await executions.record(
            intent=intent,
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
            await db[SHARED_INTENTS].update_one(
                {"intent_id": intent_id},
                {"$set": {
                    "gate_state": "blocked",
                    "last_submit_ts": _now_iso(),
                    "last_submit_by": AUTO_ROUTER_EMAIL,
                    "risk_reason": rc.reason,
                    # Doctrine 2026-07-12 (Step 7): persist reason
                    # code on every blocked branch. Was silent —
                    # only `risk_reason` was written, `broker_reason`
                    # was None, which hid the failure from operator
                    # dashboards keyed on `broker_reason`.
                    "broker_reason": "RISK_REJECTED",
                    "broker_error_bucket": "risk",
                    "broker_error_detail": str(rc.reason)[:500],
                    "notional_source": notional_source,
                }},
            )
        except Exception:  # noqa: BLE001
            pass
        return {"verdict": "blocked", "reason": rc.reason}

    # Adopt risk's authoritative notional. For crypto this is a no-op
    # (cap-authority guard above already ensured floor ≤ cap, so risk
    # doesn't clip). For equity, this is where risk's silent per-order
    # cap actually takes effect — the broker receives the CLIPPED value,
    # never the raw governor-scaled value. Without this reassignment,
    # a $100 equity intent with $10 cap would ship as $100 to the
    # broker while the executions row records $10.
    final_notional = rc.notional_usd

    # ── 3a. Equity market-closed pre-flight (2026-07-06) ────────────
    # Doctrine (operator, 2026-07-06): market_closed is NOT a broker
    # error, it's a known routing condition. Skip the Webull round-
    # trip entirely for equity intents outside RTH (or the extended-
    # hours window when the operator has flipped that flag on).
    #
    # Prior behavior: every Sunday equity intent hit Webull, got HTTP
    # 417 "The time you sent is not supported" (Webull's weekend
    # rejection), was classified `bucket=market_closed`, and was
    # terminal-stamped. Cost: ~2,000 wasted Webull calls/day, 19,223
    # error log lines, and a rising 429 rate-limit risk that
    # threatened the next RTH open.
    #
    # Now: check ET market hours locally BEFORE calling the broker.
    # Write an honest audit row (broker_status=market_closed_preflight,
    # NOT `broker_error:market_closed`) so the funnel shows the true
    # blocker without polluting broker-error metrics. Crypto lane is
    # untouched (Kraken trades 24/7).
    lane = (intent.get("lane") or "").lower()
    if lane == "equity":
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
                intent=intent,
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
                await db[SHARED_INTENTS].update_one(
                    {"intent_id": intent_id},
                    {"$set": {
                        "gate_state": "blocked",
                        "last_submit_ts": _now_iso(),
                        "last_submit_by": AUTO_ROUTER_EMAIL,
                        "broker_reason": "market_closed_preflight",
                        "broker_error_bucket": "market_closed",
                        "broker_error_detail": reason[:500],
                        "notional_source": notional_source,
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

    # ── 3b. Ladder-aware sizing + capital-ledger reserve ────────────
    # 2026-02-20 (P1 wire-up of the Per-Lane Capital Cap Ledger).
    #
    # Resolve the (brain, lane) ladder stage → route so we know
    # whether this intent is a LIVE broker submission or an
    # observation/paper receipt. Only `live_micro` and `live_normal`
    # reserve against the capital ledger — routes `observe` /
    # `paper` skip the ledger entirely (no real capital at risk).
    #
    # This gate runs AFTER the market-closed preflight so equity
    # intents outside RTH short-circuit without ever touching the
    # ledger — no phantom reservations on weekends.
    #
    # Race note: `evaluate_sizing_with_ladder` runs INSIDE the same
    # tick as the broker submit. Any ladder promotion arriving
    # mid-tick lands on the NEXT intent; this one uses the stage
    # that was active at the top of _route_one.
    ledger_reserved = False
    ledger_reserve_amount = 0.0
    ledger_lane = (intent.get("lane") or "").lower()
    try:
        from shared.sizing_gate import evaluate_sizing_with_ladder  # noqa: WPS433
        sizing = await evaluate_sizing_with_ladder(
            requested_usd=final_notional,
            brain=(intent.get("stack") or intent.get("stack_canonical") or ""),
            lane=(intent.get("lane") or None),
        )
        action = str(intent.get("action") or "").upper()
        route_is_live = sizing.route in LIVE_ROUTES

        # Ledger integration is NON-authoritative on sizing —
        # `final_notional` is already the auth notional post
        # risk.check + apply_floor. We only READ `sizing.route` to
        # decide whether the intent is a live-broker submission
        # (reserves) or observe/paper (skips). Stamp the resolved
        # provenance for audit; do NOT re-clamp notional here.
        if route_is_live:
            try:
                await db[SHARED_INTENTS].update_one(
                    {"intent_id": intent_id},
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
        # `observe` / `paper` fall through unchanged (no reserve).
        # Exit actions (SELL / COVER) also skip the reserve — they
        # RELEASE the entry's reservation on position close.
        if (
            route_is_live
            and action in ENTRY_ACTIONS
            and ledger_lane in ("equity", "crypto")
            and final_notional > 0
        ):
            from shared.capital.ledger import (  # noqa: WPS433
                get_lane_headroom, reserve_capital,
            )
            # Skip the ledger gate if this lane has not been
            # initialised. Fail-safe: uninit ledger MUST NOT block
            # live intents — the operator sees the boot warning,
            # the ledger just isn't yet enforcing caps for that
            # lane. Prevents test-suite pollution and any lifespan-
            # init failure from cascading into a full trading halt.
            head = await get_lane_headroom(ledger_lane)
            if head is None:
                logger.debug(
                    "auto_router capital_ledger SKIP — lane=%s "
                    "not initialised", ledger_lane,
                )
            else:
                ledger_reserve_amount = final_notional
                ok = await reserve_capital(
                    lane=ledger_lane,
                    amount=ledger_reserve_amount,
                    intent_id=intent_id,
                )
                if not ok:
                    # Cap exceeded — DO NOT reach the broker.
                    logger.warning(
                        "auto_router capital_ledger REJECTED "
                        "intent=%s lane=%s amount=%.2f — cap exceeded",
                        intent_id, ledger_lane, ledger_reserve_amount,
                    )
                    await executions.record(
                        intent=intent,
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
                            f"cap exceeded lane={ledger_lane} "
                            f"requested={ledger_reserve_amount:.2f}"
                        ),
                        ok=False,
                    )
                    try:
                        await db[SHARED_INTENTS].update_one(
                            {"intent_id": intent_id},
                            {"$set": {
                                "gate_state": "blocked",
                                "last_submit_ts": _now_iso(),
                                "last_submit_by": AUTO_ROUTER_EMAIL,
                                "broker_reason": "REJECTED_CAP_EXCEEDED",
                                "broker_error_bucket": "capital_ledger_cap",
                                "notional_source": notional_source,
                            }},
                        )
                    except Exception:  # noqa: BLE001
                        pass
                    return {
                        "verdict": "blocked",
                        "reason": "REJECTED_CAP_EXCEEDED",
                        "lane": ledger_lane,
                        "requested_notional": ledger_reserve_amount,
                    }
                ledger_reserved = True
    except Exception as exc:  # noqa: BLE001
        # Sizing / ledger integration is defensive — never let a
        # module-import / db issue block the broker path.
        logger.debug(
            "auto_router sizing/ledger gate skipped intent=%s: %r",
            intent_id, exc,
        )

    # ── 3. Broker ────────────────────────────────────────────────
    from shared.broker_router import (  # noqa: WPS433
        BrokerRouteBlocked, route_order,
    )
    try:
        order = await route_order(
            intent,
            notional_usd=final_notional,
            client_order_id=f"ar-{intent_id[:24]}",
        )
    except BrokerRouteBlocked as exc:
        await executions.record(
            intent=intent,
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
            await db[SHARED_INTENTS].update_one(
                {"intent_id": intent_id},
                {"$set": {
                    "gate_state": "blocked",
                    "last_submit_ts": _now_iso(),
                    "last_submit_by": AUTO_ROUTER_EMAIL,
                    "broker_reason": str(exc)[:500],
                    "notional_source": notional_source,
                }},
            )
        except Exception:  # noqa: BLE001
            pass
        return {"verdict": "blocked", "reason": str(exc)}
    except Exception as exc:  # noqa: BLE001
        exc_type = type(exc).__name__
        exc_msg = str(exc)[:1000]

        # ── Broker-error taxonomy (2026-02-17 doctrine) ────────
        # Classify the failure. Deterministic buckets (market_closed,
        # insufficient_funds, min_order_notional, invalid_order_args,
        # auth_or_permission) are TERMINAL on the first attempt.
        # Transient buckets (rate_limited, network_transient, unknown)
        # get retried up to AUTO_ROUTER_MAX_BROKER_RETRIES times
        # before being terminated with `broker_retry_exhausted`.
        # No intent retries indefinitely — that was the pre-2026-02-17
        # bug that head-of-lined the queue on Sunday's market_closed.
        from shared.broker_error_taxonomy import classify  # noqa: WPS433
        err = classify(exc)
        retry_count_before = int(intent.get("broker_retry_count") or 0)

        logger.error(
            "auto_router broker call raised intent=%s symbol=%s action=%s "
            "exc=%s bucket=%s terminal=%s retry_count=%d msg=%s",
            intent_id, intent.get("symbol"), intent.get("action"),
            exc_type, err.bucket, err.is_terminal,
            retry_count_before, exc_msg,
        )
        await executions.record(
            intent=intent,
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

        # Decide the terminal disposition of the INTENT itself.
        should_terminate = err.is_terminal
        terminal_reason = err.bucket
        if not err.is_terminal:
            new_retry_count = retry_count_before + 1
            if new_retry_count >= AUTO_ROUTER_MAX_BROKER_RETRIES:
                should_terminate = True
                terminal_reason = "broker_retry_exhausted"

        if should_terminate:
            # Release ledger reservation (if held) — broker terminally
            # rejected the order, capital is no longer at risk.
            if ledger_reserved:
                try:
                    from shared.capital.ledger import release_capital  # noqa: WPS433
                    await release_capital(
                        lane=ledger_lane,
                        intent_id=intent_id,
                        amount=ledger_reserve_amount,
                        reason="broker_terminal_reject",
                    )
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "auto_router: release_capital failed on "
                        "broker terminal intent=%s", intent_id,
                    )
            try:
                await db[SHARED_INTENTS].update_one(
                    {"intent_id": intent_id},
                    {"$set": {
                        "gate_state": "blocked",
                        "last_submit_ts": _now_iso(),
                        "last_submit_by": AUTO_ROUTER_EMAIL,
                        "broker_reason": terminal_reason,
                        "broker_error_detail": err.detail,
                        "broker_error_bucket": err.bucket,
                        "notional_source": notional_source,
                    }},
                )
            except Exception:  # noqa: BLE001
                pass

            # 2026-07-09 live-learning capture (Stage 1): broker
            # rejects are as valuable as fills for the training set.
            # Best-effort — never crash the reject path.
            try:
                from shared.learning.live_loop import capture_experience  # noqa: WPS433
                learn_intent = dict(intent)
                learn_intent.setdefault("execution", {})
                learn_intent["execution"]["action"] = action_upper
                learn_intent["execution"]["notional_usd"] = final_notional
                learn_intent["final_notional_usd"] = final_notional
                learn_intent["notional_source"] = notional_source
                await capture_experience(
                    db,
                    intent=learn_intent,
                    broker_receipt={
                        "status": "rejected",
                        "broker": err.detail.get("broker") if hasattr(err, "detail") and isinstance(err.detail, dict) else None,
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

        # Transient path — bump the retry counter and leave the intent
        # eligible for the next tick. Runs a bounded number of times
        # before the `should_terminate` branch above catches it.
        try:
            await db[SHARED_INTENTS].update_one(
                {"intent_id": intent_id},
                {"$set": {
                    "last_submit_ts": _now_iso(),
                    "last_submit_by": AUTO_ROUTER_EMAIL,
                    "broker_error_bucket": err.bucket,
                    "broker_error_detail": err.detail,
                    "notional_source": notional_source,
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

    # ── 4. Success ───────────────────────────────────────────────
    # Doctrine (2026-02-28): the audit trail, return payload, and log
    # line all record what ACTUALLY shipped to the broker — i.e.
    # `final_notional` (= rc.notional_usd for equity; = far.notional_usd
    # after pair-floor for crypto). Pre-fix code stamped `rc.notional_usd`
    # in all three places, which lied by up-to-3x when the pair-floor
    # sized crypto orders up.
    shipped_notional = final_notional
    await db[SHARED_INTENTS].update_one(
        {"intent_id": intent_id},
        {"$set": {
            "executed": True,
            "executed_at": _now_iso(),
            "executed_by": AUTO_ROUTER_EMAIL,
            "gate_state": "submitted",
            "final_notional_usd": shipped_notional,
            # 2026-07-09 audit trail (operator directive): persist which
            # code path resolved the notional so the post-mortem can
            # distinguish brain-sized orders from $5 micro-probes.
            # Values: brain_legacy | brain_v3 | micro_live_default |
            # env_default.
            "notional_source": notional_source,
            "notional_usd": notional_raw,
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
        intent=intent,
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

    # 2026-07-09 live-learning capture (Stage 1, operator directive):
    # Every intent that reached the broker door — fill OR reject —
    # is training material. Best-effort; a failure to write the
    # learning row must NEVER kill an in-progress order.
    try:
        from shared.learning.live_loop import capture_experience  # noqa: WPS433
        learning_intent = dict(intent)
        learning_intent.setdefault("execution", {})
        learning_intent["execution"]["action"] = action_upper
        learning_intent["execution"]["notional_usd"] = shipped_notional
        learning_intent["final_notional_usd"] = shipped_notional
        learning_intent["notional_source"] = notional_source
        learning_intent["broker_order"] = {
            k: order.get(k) for k in (
                "id", "order_id", "broker", "status",
                "filled_qty", "filled_avg_price",
            ) if order.get(k) is not None
        }
        await capture_experience(
            db,
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
        intent_id, intent.get("symbol"), intent.get("action"),
        shipped_notional, order.get("broker"),
        order.get("id") or order.get("order_id"),
    )
    return {
        "verdict": "executed",
        "intent_id": intent_id,
        "final_notional": shipped_notional,
        "notional_usd": shipped_notional,
        "broker": order.get("broker"),
        "order_id": order.get("id") or order.get("order_id"),
    }


# ─── Reconciliation & expiration sweeps (extracted 2026-02-19) ────
# Moved to `shared/auto_router_reconciliation.py` on 2026-02-19 to
# shrink this file from 1761 → ~1245 lines. Re-imported here so
# existing callers (tests, routes, supervisor) can keep using
# `shared.auto_router._sweep_expired_unrouted`, `._sweep_submitted_broker_orders`,
# `._finish_sweep`, `._minutes_since_iso` without changes.
from shared.auto_router_reconciliation import (  # noqa: E402
    _finish_sweep,
    _minutes_since_iso,
    _sweep_expired_unrouted,
    _sweep_submitted_broker_orders,
)


# ─── Supervisor loop (extracted 2026-02-19) ───────────────────────
# Moved `_tick`, `_loop`, `get_status`, `force_one_tick`,
# `start_auto_router_if_enabled`, `stop_auto_router` and their
# module state (`_TASK`, `_TICK_COUNT`, `_LAST_TICK_*`,
# `_STARTED_AT`) to `shared/auto_router_supervisor.py`. The
# supervisor calls back into THIS module (via attribute lookup on
# `shared.auto_router`) for `_route_one`, `_is_master_switch_armed`,
# and the reconciliation sweeps — that preserves the monkeypatch
# contract for the test suite.
#
# Re-import here so external callers (routes, tests) can keep
# using `from shared.auto_router import get_status, force_one_tick, ...`
# without any changes.
from shared.auto_router_supervisor import (  # noqa: E402
    _loop,
    _tick,
    force_one_tick,
    get_status,
    start_auto_router_if_enabled,
    stop_auto_router,
)


