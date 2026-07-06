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
# `tests/test_auto_router_max_per_tick.py` pins this contract.
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
    notional_raw = float(
        intent.get("requested_notional_usd") or AUTO_ROUTER_NOTIONAL_USD
    )

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
