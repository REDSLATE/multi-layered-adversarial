"""Auto-router supervisor loop — extracted from `shared/auto_router.py`
on 2026-02-19 (P3 refactor iteration 2).

The routing hot path (`_route_one`) stays in `auto_router.py`.
Everything that surrounds it — the scheduled tick, the loop task
supervisor, and the operator-visible introspection surface — lives
here.

Public API (re-exported from `shared.auto_router` for backward
compatibility with existing callers and tests):

    _tick() -> list[dict]
    _loop() -> None                        (task body; internal)
    get_status() -> dict
    force_one_tick() -> dict
    start_auto_router_if_enabled() -> None
    stop_auto_router() -> None

Module state (also re-exported for tests):
    _TASK, _TICK_COUNT, _LAST_TICK_TS, _LAST_TICK_RESULTS,
    _LAST_TICK_EXECUTED, _LAST_TICK_ERROR, _STARTED_AT

Callback contract to `shared.auto_router`:
    * `_route_one(intent)` — routing hot path; called per intent.
    * `_is_master_switch_armed()` — preflight gate.
    * `_sweep_expired_unrouted()`, `_sweep_submitted_broker_orders()`
      — reconciliation sweeps (also re-exported by auto_router
      from `auto_router_reconciliation`).

All 4 callbacks are resolved via ATTRIBUTE LOOKUP on
`shared.auto_router` at call time — never captured at import time.
This preserves the monkeypatch contract:
`monkeypatch.setattr(auto_router, "_route_one", mock)` swaps the
reference our tick uses.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import shared.auto_router as _ar
from db import db
from namespaces import SHARED_INTENTS

logger = logging.getLogger("auto_router.supervisor")

# ── Tunables — env-driven, mirrored from auto_router for local
# use so we don't reach into `_ar.CONST` on every tick.
AUTO_ROUTER_ENABLED = os.environ.get("AUTO_ROUTER_ENABLED", "true").lower() == "true"
AUTO_ROUTER_INTERVAL_SEC = int(os.environ.get("AUTO_ROUTER_INTERVAL_SEC", "30"))
AUTO_ROUTER_NOTIONAL_USD = float(os.environ.get("AUTO_ROUTER_NOTIONAL_USD", "10"))
AUTO_ROUTER_MAX_PER_TICK = int(os.environ.get("AUTO_ROUTER_MAX_PER_TICK", "5"))

# ── Loop task + heartbeat state ──────────────────────────────────
_TASK: Optional[asyncio.Task] = None
_TICK_COUNT: int = 0
_LAST_TICK_TS: Optional[str] = None
_LAST_TICK_RESULTS: int = 0
_LAST_TICK_EXECUTED: int = 0
_LAST_TICK_ERROR: Optional[str] = None
_LAST_TICK_TIMEOUTS: int = 0
_LAST_TICK_DEFERRED: int = 0
_LAST_TICK_DISARMED: bool = False
_LAST_TICK_EXCEPTIONS: int = 0
_LAST_INTENT_ERROR: Optional[str] = None
_STARTED_AT: Optional[str] = None

# Route-phase wall budget per tick. 5 intents × 20s each could hit
# 100s — far past the old 45s whole-tick bound, which fired
# TimeoutError and threw away ALL results ("0 picked"). Now the
# route loop self-bounds: intents past the budget are DEFERRED to
# the next tick instead of blowing up the tick.
ROUTE_BUDGET_SEC = float(os.environ.get("AUTO_ROUTER_ROUTE_BUDGET_SEC", "35"))
ROUTE_TIMEOUT_POISON_LIMIT = 3


async def _stamp_route_timeout(intent: dict) -> None:
    """Record a per-intent route timeout. After 3 strikes the intent
    is terminally stamped `blocked/ROUTE_TIMEOUT_POISON` so one hung
    symbol can't head-of-line-block the queue forever."""
    iid = intent.get("intent_id")
    if not iid:
        return
    strikes = int(intent.get("route_timeouts") or 0) + 1
    update: dict = {
        "$inc": {"route_timeouts": 1},
        "$set": {"last_route_timeout_at": _now_iso()},
    }
    if strikes >= ROUTE_TIMEOUT_POISON_LIMIT:
        update["$set"].update({
            "gate_state": "blocked",
            "broker_reason": "ROUTE_TIMEOUT_POISON",
            "broker_error_bucket": "timeout",
        })
    try:
        await asyncio.wait_for(
            db[SHARED_INTENTS].update_one({"intent_id": iid}, update),
            timeout=5.0,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("route-timeout stamp failed intent=%s: %s", iid, exc)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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

    2026-02-19: MASTER SWITCH PREFLIGHT. The operator's arm gate
    (`trading_controls.enabled` in Mongo, written by
    `POST /api/admin/trading/toggle` and `/arm`) now short-circuits
    the tick. When disarmed we STILL run the reconcile sweep — an
    in-flight submitted order must not be stranded just because the
    operator flipped the switch mid-flight.
    """
    global _LAST_TICK_TIMEOUTS, _LAST_TICK_DEFERRED, _LAST_TICK_EXCEPTIONS
    global _LAST_INTENT_ERROR
    _LAST_TICK_TIMEOUTS = 0
    _LAST_TICK_DEFERRED = 0
    _LAST_TICK_EXCEPTIONS = 0
    # Sweep first — cheap update_many, keeps the funnel honest.
    # Attribute lookup on _ar so monkeypatched mocks are picked up.
    try:
        await asyncio.wait_for(_ar._sweep_expired_unrouted(), timeout=10.0)
    except asyncio.TimeoutError:
        logger.warning("expired-unrouted sweep exceeded 10s timeout")
    except Exception as exc:  # noqa: BLE001
        logger.warning("expired-unrouted sweep raised unexpectedly: %s", exc)
    # Reconcile submitted broker orders (2026-07-06). Independently
    # timeout-guarded; a broker outage cannot block routing.
    try:
        await asyncio.wait_for(_ar._sweep_submitted_broker_orders(), timeout=15.0)
    except asyncio.TimeoutError:
        logger.warning("reconcile sweep exceeded 15s timeout")
    except Exception as exc:  # noqa: BLE001
        logger.warning("reconcile sweep raised unexpectedly: %s", exc)

    # MASTER-SWITCH PREFLIGHT — read the Mongo arm doc. If disarmed,
    # we still reconciled (above) but do NOT ingest new intents.
    # 2026-07-20: the skip is now VISIBLE — `last_tick_disarmed=true`
    # on the status payload. "Everything stays pending forever with
    # zero errors" was this branch hiding in plain sight.
    global _LAST_TICK_DISARMED
    if not await _ar._is_master_switch_armed():
        _LAST_TICK_DISARMED = True
        return []
    _LAST_TICK_DISARMED = False

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
        # Poison guard: 3 route timeouts and you're out of the queue
        # ($not matches docs where the field is missing too).
        "route_timeouts": {"$not": {"$gte": ROUTE_TIMEOUT_POISON_LIMIT}},
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
    loop_time = asyncio.get_event_loop().time
    deadline = loop_time() + ROUTE_BUDGET_SEC
    for intent in sample:
        remaining = deadline - loop_time()
        if remaining < 3.0:
            _LAST_TICK_DEFERRED += 1
            continue
        try:
            # Per-intent bound, capped by the tick's remaining route
            # budget — a slow broker call slips to the next tick
            # instead of killing this one.
            r = await asyncio.wait_for(
                _ar._route_one(intent), timeout=min(20.0, remaining),
            )
            results.append(r)
            if r.get("verdict") == "executed":
                logger.info(
                    "auto-routed %s %s %s -> $%s",
                    intent.get("stack"), intent.get("action"),
                    intent.get("symbol"),
                    r.get("final_notional") or r.get("notional_usd") or 0,
                )
        except asyncio.TimeoutError:
            _LAST_TICK_TIMEOUTS += 1
            logger.error(
                "auto-router _route_one timeout intent=%s symbol=%s action=%s",
                intent.get("intent_id"), intent.get("symbol"), intent.get("action"),
            )
            await _stamp_route_timeout(intent)
        except Exception as e:  # noqa: BLE001
            # 2026-07-20: per-intent crashes are no longer invisible.
            # "0 picked, no error" on the tile while route_one throws
            # every tick was undiagnosable from the UI.
            _LAST_TICK_EXCEPTIONS += 1
            _LAST_INTENT_ERROR = f"{type(e).__name__}: {e}"[:300]
            logger.exception(
                "auto-router error on intent %s: %s",
                intent.get("intent_id"), e,
            )
    return results


async def _loop() -> None:
    global _STARTED_AT, _TICK_COUNT, _LAST_TICK_TS, _LAST_TICK_RESULTS
    global _LAST_TICK_EXECUTED, _LAST_TICK_ERROR
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
            # 90s safety net only — the tick now self-bounds every
            # phase (sweeps 10+15s, query 12s, route budget 35s).
            results = await asyncio.wait_for(_tick(), timeout=90.0)
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
        "last_tick_route_timeouts": _LAST_TICK_TIMEOUTS,
        "last_tick_deferred": _LAST_TICK_DEFERRED,
        "last_tick_disarmed": _LAST_TICK_DISARMED,
        "master_switch_read_degraded": bool(getattr(_ar, "_ARM_READ_DEGRADED", False)),
        "master_switch_last_known": getattr(_ar, "_ARM_LAST_GOOD", None),
        "master_switch_read_error": getattr(_ar, "_ARM_LAST_READ_ERROR", None),
        "last_tick_exceptions": _LAST_TICK_EXCEPTIONS,
        "last_intent_error": _LAST_INTENT_ERROR,
        "last_route_stage_trace": getattr(_ar, "_LAST_STAGE_TRACE", None) or None,
        "route_budget_sec": ROUTE_BUDGET_SEC,
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
    global _TICK_COUNT, _LAST_TICK_TS, _LAST_TICK_RESULTS
    global _LAST_TICK_EXECUTED, _LAST_TICK_ERROR
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
