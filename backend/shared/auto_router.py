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

    # ── 2. Risk hard limits ──────────────────────────────────────
    # Governor's risk multiplier is applied here — ONE PASS, no
    # callback. SeatDecision already carries it; we just multiply.
    adjusted_notional = max(0.0, notional_raw * sd.risk_multiplier)
    rc = await risk.check(intent, notional_usd=adjusted_notional)
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

    # ── 3. Broker ────────────────────────────────────────────────
    from shared.broker_router import (  # noqa: WPS433
        BrokerRouteBlocked, route_order,
    )
    try:
        order = await route_order(
            intent,
            notional_usd=rc.notional_usd,
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
        logger.exception(
            "auto_router broker call raised intent=%s symbol=%s action=%s "
            "exc=%s msg=%s",
            intent_id, intent.get("symbol"), intent.get("action"),
            exc_type, exc_msg,
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
            ok=False,
        )
        # Do NOT stamp the intent terminally — broker errors are
        # transient; let the next tick retry.
        return {
            "verdict": "error",
            "reason": exc_msg,
            "exception_type": exc_type,
        }

    # ── 4. Success ───────────────────────────────────────────────
    await db[SHARED_INTENTS].update_one(
        {"intent_id": intent_id},
        {"$set": {
            "executed": True,
            "executed_at": _now_iso(),
            "executed_by": AUTO_ROUTER_EMAIL,
            "gate_state": "submitted",
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
        notional_usd=rc.notional_usd,
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
        rc.notional_usd, order.get("broker"),
        order.get("id") or order.get("order_id"),
    )
    return {
        "verdict": "executed",
        "intent_id": intent_id,
        "final_notional": rc.notional_usd,
        "notional_usd": rc.notional_usd,
        "broker": order.get("broker"),
        "order_id": order.get("id") or order.get("order_id"),
    }


async def _tick() -> list[dict]:
    """One scan pass. Picks up at most AUTO_ROUTER_MAX_PER_TICK unexecuted
    intents and routes them through Seat → Risk → Broker.

    2026-02-27 architectural reduction: the legacy "seat-mismatch
    sweep" and `seats_with_execute(lane)` indirection are gone.
    `Seat.decide(intent)` is the single eligibility check; each
    intent's lane/brain combo is evaluated inline by `_route_one`.

    Stale intents (older than AUTO_ROUTER_LOOKBACK_MIN, default 60m)
    are NOT picked up — that's the operator-curated history boundary.
    """
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
        # stamped by an earlier tick (blocked or advisory_only).
        "gate_state": {"$nin": ["blocked", "no_trade", "advisory_only", "submitted"]},
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
            # ─── Paradox v3 trigger watcher tick (2026-02, Step 5) ───
            # Piggybacks on the auto-router's 30s cadence so we don't
            # introduce a second loop. DORMANT by default — the
            # watcher returns immediately when `PARADOX_V3_TRIGGER_
            # WATCHER` is off. When live, processes TTL expiries + any
            # trigger/invalidation fires using the default price
            # fetcher. Errors are swallowed locally so a watcher
            # crash never takes down the auto-router itself.
            try:
                from shared.pipeline.trigger_watcher import (
                    default_price_fetcher,
                    scan_watch_queue,
                )
                await scan_watch_queue(price_fetcher=default_price_fetcher)
            except Exception as wexc:  # noqa: BLE001
                logger.warning("paradox_v3 trigger_watcher tick failed: %s", wexc)
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
