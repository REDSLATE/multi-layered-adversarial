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
from typing import Optional

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
    """The new path (2026-02-27 architectural reduction).

        Brain (already emitted) → Seat → Risk → Broker → Executions audit

    No dry-run. No auto-submit policy. No council. No consensus pool.
    No legacy brain wrappers. No unified pipeline. One row in the
    `executions` collection per attempt, period.

    Returns a verdict dict in the legacy shape so existing callers
    (status endpoint, post-mortem aggregator) keep working unchanged.
    """
    from shared import executions, risk, seat  # noqa: WPS433

    intent_id = intent.get("intent_id") or ""
    # ── Notional resolution ──────────────────────────────────────
    # Legacy path stamps `requested_notional_usd`; v3 envelope stamps
    # `execution.notional_usd`. Try both, in that order. If the brain
    # emitted a directional (BUY/SELL) intent with NO notional on either
    # slot, apply the micro-live default so the pipeline can send a
    # $5 probe order. Doctrine (2026-07-09 operator directive):
    #
    #   "direction exists now. The next executable choke is
    #    notional_usd=null … Add a micro-notional fallback."
    #
    # The `notional_source` string rides on the intent doc so the
    # post-mortem can distinguish brain-sized orders from micro-probes.
    # ── Notional resolution (2026-07-09 revised operator directive) ──
    # Doctrine:
    #
    #   Market data
    #     → brain chooses BUY/SELL
    #     → doctrine scores quality
    #     → executor assigns notional  ← THIS BLOCK
    #     → capital ledger reserves
    #     → broker submits
    #
    # Rule (assign_micro_notional):
    #   1. If the brain already sized the intent (legacy or v3), USE IT.
    #      → notional_source ∈ {"brain_legacy", "brain_v3"}
    #   2. Directional intent (BUY/SELL) with no size AND doctrine
    #      flagged any failed checks → $1 quality-weak probe.
    #      → notional_source = "micro_probe_failed_quality"
    #   3. Directional intent (BUY/SELL) with no size AND doctrine is
    #      clean (no failed checks) → $5 default probe.
    #      → notional_source = "micro_default"
    #   4. Non-directional (HOLD/...) → env default ($10).
    #      → notional_source = "env_default"
    #
    # The `notional_source` string rides on the intent doc so the
    # post-mortem can distinguish brain-sized orders from probes and,
    # for probes, whether doctrine passed or flagged them as weak.
    _exec = intent.get("execution") or {}
    action_upper = str(intent.get("action") or "").upper()
    v3_notional = _exec.get("notional_usd") if isinstance(_exec, dict) else None
    legacy_notional = intent.get("requested_notional_usd")
    notional_source: str
    if legacy_notional not in (None, 0, 0.0):
        notional_raw = float(legacy_notional)
        notional_source = "brain_legacy"
    elif v3_notional not in (None, 0, 0.0):
        notional_raw = float(v3_notional)
        notional_source = "brain_v3"
    elif action_upper in {"BUY", "SELL"}:
        # Brain made a directional move but didn't size it.
        # Consult the doctrine packet — if ANY quality checks failed,
        # ship a $1 probe; otherwise a $5 default probe.
        try:
            dp = intent.get("doctrine_packet") or {}
            seats_dp = (dp.get("seats") or {}) if isinstance(dp, dict) else {}
            ej = seats_dp.get("execution_judge") or {}
            _failed = list(ej.get("failed_checks") or [])
        except Exception:  # noqa: BLE001
            _failed = []

        if _failed:
            notional_raw = float(
                os.environ.get("MICRO_PROBE_FAILED_QUALITY_USD", "1.00")
            )
            notional_source = "micro_probe_failed_quality"
        else:
            notional_raw = float(
                os.environ.get("MICRO_LIVE_DEFAULT_USD", "5.00")
            )
            notional_source = "micro_default"
    else:
        notional_raw = AUTO_ROUTER_NOTIONAL_USD
        notional_source = "env_default"

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
        terminal_state = (
            "advisory_only" if sd.verdict == "pass" else "blocked"
        )
        try:
            await db[SHARED_INTENTS].update_one(
                {"intent_id": intent_id},
                {"$set": {
                    "gate_state": terminal_state,
                    "last_submit_ts": _now_iso(),
                    "last_submit_by": AUTO_ROUTER_EMAIL,
                    "seat_reason": sd.reason,
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


async def _sweep_expired_unrouted() -> int:
    """Terminally stamp intents older than `AUTO_ROUTER_EXPIRE_MIN` that
    were never routed to a terminal state.

    Rationale (2026-02-28): `_tick` only SAMPLES within
    `AUTO_ROUTER_LOOKBACK_MIN` (60 min by default). Anything older that
    hadn't already been stamped `blocked` / `advisory_only` / `submitted`
    silently vanished from the funnel — the operator saw the intent
    emitted, then nothing. This sweeper closes that gap by writing an
    explicit `gate_state=expired_unrouted` on age-outs.

    Default expire window is DOUBLE the lookback (120 min vs 60 min) so
    a legitimate late-arriving intent isn't cut off by racing the two
    windows. Returns the number of intents stamped this pass.
    """
    try:
        expire_min = int(os.environ.get("AUTO_ROUTER_EXPIRE_MIN",
                                        str(AUTO_ROUTER_EXPIRE_MIN)))
    except (TypeError, ValueError):
        expire_min = AUTO_ROUTER_EXPIRE_MIN
    expire_cutoff = (
        datetime.now(timezone.utc) - timedelta(minutes=expire_min)
    ).isoformat()
    try:
        # 2026-02-28: hard batch cap + Mongo-side deadline. On prod's
        # multi-million-row `shared_intents`, an unbounded update_many
        # over `ingest_ts < cutoff` could scan a huge slice and blow
        # past the 12s asyncio timeout below. We DELIBERATELY cap at
        # 500 stamps per tick — old-and-not-terminal intents leak
        # slowly over successive ticks instead of one giant sweep
        # that could starve the routing scan (same collection).
        # `max_time_ms(3000)` fails at the DB layer if the query
        # can't complete within 3s, well under the asyncio deadline.
        expired_ids: list[str] = []
        cur = (
            db[SHARED_INTENTS]
            .find(
                {
                    "ingest_ts": {"$lt": expire_cutoff},
                    "executed": {"$ne": True},
                    "gate_state": {"$nin": [
                        "blocked", "no_trade", "advisory_only",
                        "submitted", "expired_unrouted",
                    ]},
                },
                {"intent_id": 1, "_id": 0},
            )
            .max_time_ms(3000)
            .limit(500)
        )
        async for d in cur:
            if d.get("intent_id"):
                expired_ids.append(d["intent_id"])
        if not expired_ids:
            return 0
        result = await asyncio.wait_for(
            db[SHARED_INTENTS].update_many(
                {"intent_id": {"$in": expired_ids}},
                {"$set": {
                    "gate_state": "expired_unrouted",
                    "expired_at": _now_iso(),
                    "expired_by": AUTO_ROUTER_EMAIL,
                    "expire_reason": (
                        f"aged_past_router_window:{expire_min}min"
                    ),
                }},
            ),
            timeout=5.0,
        )
        stamped = int(result.modified_count or 0)
        if stamped:
            logger.info(
                "auto_router expired_unrouted sweep stamped %d intents "
                "older than %d min", stamped, expire_min,
            )
        return stamped
    except Exception as exc:  # noqa: BLE001
        logger.warning("expired_unrouted sweep failed: %s", exc)
        return 0


# ─── Broker reconciliation sweep (2026-07-06) ─────────────────────────
# Poll Webull for the current status of intents MC submitted but whose
# fills/rejects haven't been observed. Fixes P1 stuck-`submitted` bug
# from the handoff: intents that got submitted but later filled or
# rejected by the broker were never transitioning to their terminal
# state — MC had no closed-loop reconciliation.
#
# Design pins (operator sign-off 2026-07-06):
#   * Equity lane only. Kraken adapter lacks `get_order` symmetry;
#     crypto reconciliation is a separate task.
#   * Skip fresh submits (<30s old) — broker hasn't touched them.
#   * Cap 25 intents per tick to bound Webull API calls under 429 risk.
#   * `.max_time_ms(3000)` bounding, `asyncio.wait_for` timeout guards.
#   * On rejection: classify() via existing broker_error_taxonomy —
#     terminal buckets (insufficient_funds, market_closed, etc.) go
#     straight to `broker_rejected`; transient buckets get retried up
#     to RECONCILE_MAX_RETRIES (3), then also go terminal.
#   * On retry: flip `gate_state='pending'` (the canonical fresh-
#     emission state; the pipeline treats it identically). Preserve
#     `ingest_ts` — an aged-out `pending` intent still gets caught by
#     `_sweep_expired_unrouted` at 120min. Between 60min and 120min
#     it's in a stall zone but NOT silent. Log a WARNING when a
#     requeue happens on an intent already past 75% of the lookback
#     window so the operator can spot slow retry cycles.
#   * On Filled: update the intent doc only — do NOT write a new
#     executions row. The original submit-time row is the audit;
#     reconciliation just updates the fill fields.
RECONCILE_MAX_RETRIES = 3
RECONCILE_MIN_AGE_SEC = 30
RECONCILE_BATCH_CAP = 25
RECONCILE_BOUNDARY_WARN_MIN = 45  # 75% of default 60min lookback
# Minimum wall-clock gap between two sweep runs. The auto_router
# scheduled tick fires every 30s (AUTO_ROUTER_INTERVAL_SEC), but
# `force_one_tick()` is ALSO invoked out-of-band on every intent
# insert (see shared/intents.py:_run_auto_router_kick — a ~50ms
# latency optimization for fresh brain emissions). Without this
# gate, a burst of 5 intents in 7s would trigger 5 back-to-back
# reconcile sweeps → 5N Webull `get_order` calls → HTTP 429. This
# gate keeps the scheduled 30s cadence but skips the redundant
# kicker-triggered runs (2026-07-06 smoke-test finding).
RECONCILE_MIN_INTERVAL_SEC = 25
_LAST_RECONCILE_SWEEP_TS: Optional[datetime] = None


async def _sweep_submitted_broker_orders() -> dict:
    """Poll each broker for the current status of `gate_state='submitted'`
    intents. Transitions them to `filled`, `broker_rejected`, or (on
    transient reject under retry cap) back to `pending` for re-routing
    on the next tick.

    2026-07-09 iter-22 — crypto sweep landing (P2 backlog):
        Historically this function only queried Webull (`lane=equity`),
        leaving Kraken-submitted intents stuck in `submitted` state
        forever. As of iter-22 both adapters are polled — `KrakenLive
        Adapter.get_order(txid)` returns the same normalized shape
        that Webull does (see `shared.crypto.kraken._normalize_kraken
        _order`), so the FILLED / REJECTED / EXPIRED branches below
        work uniformly for both lanes.

    Returns a counts dict for observability. Never raises — a broker
    outage or DB slowness cannot crash the auto-router tick.
    """
    counts = {
        "polled": 0,
        "filled": 0,
        "rejected_terminal": 0,
        "rejected_retry": 0,
        "no_change": 0,
        "errors": 0,
        "requeue_near_boundary": 0,
        "skipped_rate_limited": 0,
        "by_lane": {"equity": 0, "crypto": 0},
    }
    # Rate-limit: skip if a sweep ran within the last
    # RECONCILE_MIN_INTERVAL_SEC seconds. Protects Webull's per-second
    # `get_order` budget when `force_one_tick()` is called back-to-
    # back from intents.py on every intent insert.
    global _LAST_RECONCILE_SWEEP_TS
    now_utc = datetime.now(timezone.utc)
    if _LAST_RECONCILE_SWEEP_TS is not None:
        elapsed = (now_utc - _LAST_RECONCILE_SWEEP_TS).total_seconds()
        if elapsed < RECONCILE_MIN_INTERVAL_SEC:
            counts["skipped_rate_limited"] = 1
            return counts
    _LAST_RECONCILE_SWEEP_TS = now_utc

    try:
        # Local imports keep the module-level import graph clean and
        # avoid any circular pull at auto_router boot.
        from shared.broker_router import get_webull_adapter  # noqa: WPS433
        from shared.crypto.broker_adapter import get_kraken_adapter  # noqa: WPS433
        from shared.broker_error_taxonomy import classify  # noqa: WPS433
    except Exception as exc:  # noqa: BLE001
        logger.warning("reconcile sweep: adapter/classify import failed: %s", exc)
        return counts

    # Resolve each lane's adapter independently — an outage on one
    # broker must NEVER wedge the sweep for the other.
    adapters: dict[str, Any] = {}
    try:
        wb = await get_webull_adapter()
        if wb is not None:
            adapters["equity"] = wb
    except Exception as exc:  # noqa: BLE001
        logger.warning("reconcile sweep: get_webull_adapter failed: %s", exc)
    try:
        kr = await get_kraken_adapter()
        if kr is not None:
            adapters["crypto"] = kr
    except Exception as exc:  # noqa: BLE001
        logger.warning("reconcile sweep: get_kraken_adapter failed: %s", exc)

    if not adapters:
        return counts

    poll_cutoff = (now_utc - timedelta(seconds=RECONCILE_MIN_AGE_SEC)).isoformat()

    # Query intents PER LANE so the equity-Webull budget and the
    # crypto-Kraken budget are drained in separate batches.
    pending_by_lane: dict[str, list[dict]] = {}
    for lane_name in adapters.keys():
        try:
            cur = (
                db[SHARED_INTENTS]
                .find(
                    {
                        "gate_state": "submitted",
                        "lane": lane_name,
                        # Webull stamps `broker_order.id`; Kraken stamps
                        # `broker_order.order_id`. Accept either — the
                        # per-intent extraction below reads both.
                        "$or": [
                            {"broker_order.id": {"$exists": True, "$ne": None}},
                            {"broker_order.order_id": {"$exists": True, "$ne": None}},
                        ],
                        "executed_at": {"$lt": poll_cutoff},
                    },
                    {
                        "_id": 0, "intent_id": 1, "symbol": 1, "action": 1,
                        "lane": 1, "stack": 1, "ingest_ts": 1, "executed_at": 1,
                        "broker_order": 1, "submit_retry_count": 1,
                        "final_notional_usd": 1, "sizing_provenance": 1,
                    },
                )
                .max_time_ms(3000)
                .limit(RECONCILE_BATCH_CAP)
            )
            rows: list[dict] = []
            async for d in cur:
                rows.append(d)
            pending_by_lane[lane_name] = rows
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "reconcile sweep: %s query failed: %s", lane_name, exc,
            )

    # Flatten to a single processing list, tagging lane on each row.
    pending_intents: list[tuple[str, dict]] = []
    for lane_name, rows in pending_by_lane.items():
        for d in rows:
            pending_intents.append((lane_name, d))

    for lane_name, intent in pending_intents:
        counts["polled"] += 1
        counts["by_lane"][lane_name] = counts["by_lane"].get(lane_name, 0) + 1
        adapter = adapters[lane_name]
        intent_id = intent.get("intent_id")
        bo_meta = intent.get("broker_order") or {}
        order_id = bo_meta.get("id") or bo_meta.get("order_id")
        if not intent_id or not order_id:
            counts["errors"] += 1
            continue

        try:
            bo = await asyncio.wait_for(
                adapter.get_order(str(order_id)),
                timeout=8.0,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "reconcile poll failed lane=%s intent=%s order_id=%s: %s",
                lane_name, intent_id, order_id, exc,
            )
            counts["errors"] += 1
            continue

        status = (bo.get("status") or "").upper()

        # ── FILLED ─────────────────────────────────────────────
        if status == "FILLED":
            try:
                await db[SHARED_INTENTS].update_one(
                    {"intent_id": intent_id},
                    {"$set": {
                        "gate_state": "filled",
                        "filled_at": _now_iso(),
                        "reconciled_by": AUTO_ROUTER_EMAIL,
                        "broker_order.status": "FILLED",
                        "broker_order.filled_qty": bo.get("filled_qty"),
                        "broker_order.filled_avg_price": bo.get("filled_avg_price"),
                        "broker_order.filled_at": bo.get("filled_at"),
                    }},
                )
                counts["filled"] += 1
                logger.info(
                    "reconcile FILLED intent=%s order_id=%s filled_qty=%s "
                    "avg_price=%s",
                    intent_id, order_id,
                    bo.get("filled_qty"), bo.get("filled_avg_price"),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "reconcile FILLED update failed intent=%s: %s",
                    intent_id, exc,
                )
                counts["errors"] += 1
            continue

        # ── REJECTED / CANCELLED / EXPIRED ─────────────────────
        if status in {"CANCELLED", "CANCELED", "REJECTED", "EXPIRED"}:
            reject_reason = str(
                bo.get("reject_reason")
                or bo.get("status_detail")
                or bo.get("last_error")
                or status
            )
            err = classify(reject_reason)
            retry_count = int(intent.get("submit_retry_count") or 0)

            if err.is_terminal or retry_count >= RECONCILE_MAX_RETRIES:
                # Release ledger reservation on terminal rejection.
                # Uses `final_notional_usd` stamped by the SUCCESS
                # path; falls back to `sizing_provenance.final_usd`.
                try:
                    from shared.capital.ledger import release_capital  # noqa: WPS433
                    lane_str = (intent.get("lane") or "").lower()
                    amount = float(
                        intent.get("final_notional_usd")
                        or (intent.get("sizing_provenance") or {}).get("final_usd")
                        or 0.0
                    )
                    if amount > 0 and lane_str in ("equity", "crypto"):
                        await release_capital(
                            lane=lane_str,
                            intent_id=intent_id,
                            amount=amount,
                            reason="broker_terminal_reject",
                        )
                except Exception:  # noqa: BLE001
                    logger.debug(
                        "reconcile: release_capital failed on terminal "
                        "reject intent=%s", intent_id,
                    )
                try:
                    await db[SHARED_INTENTS].update_one(
                        {"intent_id": intent_id},
                        {"$set": {
                            "gate_state": "broker_rejected",
                            "rejected_at": _now_iso(),
                            "reconciled_by": AUTO_ROUTER_EMAIL,
                            "broker_reason": err.bucket,
                            "broker_error_detail": err.detail,
                            "broker_error_terminal": bool(err.is_terminal),
                            "submit_retry_count": retry_count,
                        }},
                    )
                    counts["rejected_terminal"] += 1
                    logger.info(
                        "reconcile REJECTED (terminal) intent=%s bucket=%s "
                        "retries=%d/%d",
                        intent_id, err.bucket, retry_count,
                        RECONCILE_MAX_RETRIES,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "reconcile REJECTED-terminal update failed intent=%s: %s",
                        intent_id, exc,
                    )
                    counts["errors"] += 1
                continue

            # Transient reject under cap → requeue as `pending`.
            try:
                # Near-boundary check: if the intent is already >75%
                # of the way to lookback expiry, log a WARNING so the
                # operator can spot slow retry cycles that risk
                # stalling in the 60-120min zone before the expire
                # sweep catches them.
                age_min = _minutes_since_iso(intent.get("ingest_ts"), now_utc)
                near_boundary = (
                    age_min is not None
                    and age_min > RECONCILE_BOUNDARY_WARN_MIN
                )
                if near_boundary:
                    counts["requeue_near_boundary"] += 1
                    logger.warning(
                        "reconcile requeue NEAR BOUNDARY intent=%s "
                        "age_min=%.1f retry=%d/%d bucket=%s — approaching "
                        "60min lookback; expire-sweep backstop at 120min",
                        intent_id, age_min, retry_count + 1,
                        RECONCILE_MAX_RETRIES, err.bucket,
                    )

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
                            "reconciled_by": AUTO_ROUTER_EMAIL,
                        },
                        "$unset": {
                            "broker_order": "",
                            "executed_at": "",
                            "executed_by": "",
                        },
                    },
                )
                counts["rejected_retry"] += 1
                logger.info(
                    "reconcile REJECTED (retry %d/%d) intent=%s bucket=%s "
                    "→ requeued as pending",
                    retry_count + 1, RECONCILE_MAX_RETRIES,
                    intent_id, err.bucket,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "reconcile REJECTED-retry update failed intent=%s: %s",
                    intent_id, exc,
                )
                counts["errors"] += 1
            continue

        # ── PARTIAL / WORKING / SUBMITTED / PENDING_CANCEL ─────
        # Not yet terminal on the broker side; leave alone and poll
        # again next tick.
        counts["no_change"] += 1

    if counts["polled"]:
        logger.info(
            "auto_router reconcile sweep: polled=%d (equity=%d crypto=%d) "
            "filled=%d rejected_terminal=%d rejected_retry=%d no_change=%d "
            "errors=%d requeue_near_boundary=%d",
            counts["polled"],
            counts["by_lane"].get("equity", 0),
            counts["by_lane"].get("crypto", 0),
            counts["filled"],
            counts["rejected_terminal"], counts["rejected_retry"],
            counts["no_change"], counts["errors"],
            counts["requeue_near_boundary"],
        )
    return counts


def _minutes_since_iso(iso_str: Optional[str], now_utc: datetime) -> Optional[float]:
    """Best-effort parse of an ISO-8601 UTC timestamp string → age in
    minutes from `now_utc`. Returns None on parse failure so callers
    can skip the near-boundary log without crashing."""
    if not iso_str:
        return None
    try:
        # fromisoformat handles the standard "+00:00" suffix; Python's
        # ISO parser is picky about the `Z` shorthand so normalize it.
        s = iso_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (now_utc - dt).total_seconds() / 60.0
    except (TypeError, ValueError):
        return None


async def _tick() -> list[dict]:
    """One scan pass. Picks up at most AUTO_ROUTER_MAX_PER_TICK unexecuted
    intents and routes them through Seat → Risk → Broker.

    2026-02-27 architectural reduction: the legacy "seat-mismatch
    sweep" and `seats_with_execute(lane)` indirection are gone.
    `Seat.decide(intent)` is the single eligibility check; each
    intent's lane/brain combo is evaluated inline by `_route_one`.

    Stale intents (older than AUTO_ROUTER_LOOKBACK_MIN, default 60m)
    are NOT picked up by the routing sample — that's the operator-
    curated history boundary. But we DO run `_sweep_expired_unrouted`
    each tick to stamp anything past `AUTO_ROUTER_EXPIRE_MIN` (default
    120m) so aged-out intents remain visible in the funnel as
    `expired_unrouted` rather than silently vanishing.
    """
    # Sweep first — cheap update_many, and it keeps the funnel honest
    # even in ticks where the sample query returns nothing.
    await _sweep_expired_unrouted()
    # Reconcile submitted broker orders (2026-07-06). Independently
    # timeout-guarded; a broker outage cannot block routing.
    try:
        await asyncio.wait_for(_sweep_submitted_broker_orders(), timeout=15.0)
    except asyncio.TimeoutError:
        logger.warning("reconcile sweep exceeded 15s timeout")
    except Exception as exc:  # noqa: BLE001
        logger.warning("reconcile sweep raised unexpectedly: %s", exc)

    try:
        lookback_min = int(os.environ.get("AUTO_ROUTER_LOOKBACK_MIN", "60"))
    except (TypeError, ValueError):
        lookback_min = 60
    lookback_cutoff = (
        datetime.now(timezone.utc) - timedelta(minutes=lookback_min)
    ).isoformat()
    q = {
        "ingest_ts": {"$gte": lookback_cutoff},
        "executed": {"$ne": True},
        "action": {"$in": ["BUY", "SELL", "SHORT", "COVER"]},
        "symbol": {"$ne": None},
        # Honest queue: don't re-process intents already terminally
        # stamped by an earlier tick (blocked, advisory_only, submitted,
        # or aged-out via the expiration sweeper).
        "gate_state": {"$nin": [
            "blocked", "no_trade", "advisory_only", "submitted",
            "expired_unrouted",
        ]},
    }
    sample = await asyncio.wait_for(
        (
            db[SHARED_INTENTS]
            .find(q, {"_id": 0})
            .sort("ingest_ts", -1)
            .max_time_ms(8000)
            .to_list(AUTO_ROUTER_MAX_PER_TICK)
        ),
        timeout=12.0,
    )
    if not sample:
        return []

    results: list[dict] = []
    for intent in sample:
        try:
            # 2026-06-30: route_one wrapped in its own bounded timeout
            # so a slow broker call cannot block the entire tick. The
            # tick exits in ≤30s no matter what.
            r = await asyncio.wait_for(_route_one(intent), timeout=20.0)
            results.append(r)
            if r.get("verdict") == "executed":
                logger.info(
                    "auto-routed %s %s %s -> $%s",
                    intent.get("stack"), intent.get("action"),
                    intent.get("symbol"),
                    r.get("final_notional") or r.get("notional_usd") or 0,
                )
        except asyncio.TimeoutError:
            logger.error(
                "auto-router _route_one timeout intent=%s symbol=%s action=%s",
                intent.get("intent_id"), intent.get("symbol"), intent.get("action"),
            )
        except Exception as e:  # noqa: BLE001
            logger.exception(
                "auto-router error on intent %s: %s",
                intent.get("intent_id"), e,
            )
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
            # 2026-06-30 prod-hang fix: bound the entire tick so a
            # hung Mongo call cannot block the loop forever. Without
            # this the tile reads `tick_count=0 · last_tick_ts=None
            # · last_tick_error=None` indefinitely because the await
            # never returns and the try/except never fires.
            results = await asyncio.wait_for(_tick(), timeout=45.0)
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
        "pipeline": "unified",
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
