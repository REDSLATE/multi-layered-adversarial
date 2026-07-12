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
    """Orchestrator (2026-02-11, P6b-finish).

        Brain → [Master switch] → [Seat] → [Risk] → [Broker] → [Finalize]

    The body has been flattened into 5 stage functions living in
    `shared/auto_router_stages.py`. Each stage receives a shared
    `RouteContext` (see `auto_router_helpers.py`) and either returns
    None (continue) or a verdict dict (short-circuit).

    Doctrine unchanged:
      * Master-switch preflight gates manual submits too.
      * One row in `executions` per attempt (fill OR reject).
      * Per-lane capital ledger reserves BEFORE broker submit;
        terminal broker rejects release the reservation.
      * Broker taxonomy: deterministic errors terminate on first
        try; transient errors get AUTO_ROUTER_MAX_BROKER_RETRIES.
    """
    from shared.auto_router_helpers import RouteContext
    from shared.auto_router_stages import (
        _finalize_gate_state,
        _gate_master_switch,
        _gate_risk,
        _gate_seat,
        _route_and_submit,
    )

    ctx = RouteContext(intent=intent)
    ctx.finalize_inputs()

    for stage in (
        _gate_master_switch,
        _gate_seat,
        _gate_risk,
        _route_and_submit,
    ):
        verdict = await stage(ctx)
        if verdict is not None:
            return verdict

    return await _finalize_gate_state(ctx)


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


