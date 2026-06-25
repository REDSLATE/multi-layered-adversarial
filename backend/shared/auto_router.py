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
from datetime import datetime, timezone
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
    """Run an intent through the Unified Pipeline.

    Three blockers, one receipt: Seat → Governor (modifier) →
    RoadGuard → Broker. See `shared/pipeline/execution_pipeline.py`
    for the full state machine. This function is a thin wrapper —
    no extra gating, sizing, or classification happens here.

    Returns the legacy verdict dict shape so existing callers
    (status endpoint, post-mortem aggregator) keep working unchanged.
    """
    from shared.pipeline.adapter import run_unified_for_intent  # noqa: WPS433
    notional_raw = float(
        intent.get("requested_notional_usd") or AUTO_ROUTER_NOTIONAL_USD
    )
    return await run_unified_for_intent(intent, notional_raw)


async def _persist_blocked_intent(intent_id: str, notional: float, result: dict) -> None:
    """Write the auto_router_blocked gate row + mark the intent blocked.

    Only used by `_sweep_seat_mismatched_intents` to drain legacy
    limbo (intents whose lane has no current seat-holder). The
    unified pipeline writes its own receipts to `pipeline_receipts`;
    this function only touches `shared_gate_results` + `shared_intents`.
    """
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


async def _sweep_seat_mismatched_intents() -> int:
    """Doctrine (2026-05-31, position-model alignment): an intent's
    `holds_executor_seat=False` flag means "the brain that POSTED this
    intent did not hold the executor seat at post-time". Under the
    position-model doctrine, that's NOT a terminal state — authority
    lives in the seat, not the brain — so whichever brain CURRENTLY
    holds the lane's executor seat can route this intent.

    This sweep is consistent with the pipeline's seat check:
      - If the lane has a current executor-seat holder → leave the
        intent pending; the auto-router will pick it up.
      - If the lane has NO current executor-seat holder → block it
        with a typed reason so the operator queue stays honest.
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
            # eligible to fire under the pipeline's seat check.
            # Leave it pending; _tick() will pick it up next pass.
            continue
        # No holder anywhere → terminal block with a clear, lane-aware
        # reason. Operator can re-seat and the next post will succeed;
        # existing blocked intents stay blocked for audit clarity.
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

    Per-intent seat eligibility is checked against the CURRENT
    seat-holder for the intent's lane — same question the pipeline's
    Seat layer asks. So an intent posted by REDEYE while Alpha held
    the equity executor seat IS eligible to fire as long as some
    brain currently holds an equity executor seat, regardless of who.
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
        # would keep retrying gate-failed intents forever.
        "gate_state": {"$nin": ["blocked", "no_trade", "advisory_only"]},
    }
    # Pull a larger sample than AUTO_ROUTER_MAX_PER_TICK so the
    # per-intent seat-eligibility filter can drop ineligible ones
    # and still leave us with up-to-MAX_PER_TICK eligible candidates.
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
            # No current seat-holder for this lane — the pipeline's
            # Seat layer would block anyway. Skip silently; the sweep
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
                    "auto-routed %s %s %s -> %s",
                    intent.get("stack"), intent.get("action"), intent.get("symbol"),
                    r.get("final_notional") or r.get("notional_usd") or 0,
                )
            else:
                # 2026-06-22 (P0 — Seat Drift / Funnel-leak fix):
                # The Unified Pipeline writes its own receipt to
                # `pipeline_receipts`, but it does NOT update the
                # canonical `shared_intents.gate_state`. Without this
                # writeback, every BLOCKED/NO_ORDER intent stayed at
                # `gate_state=pending` and got re-evaluated on every
                # 30s tick — forever. Production funnel showed 6,459
                # of 6,464 intents (100% leak) dropped between
                # EMITTED→SEAT_APPROVED because the same 5 stuck
                # TRIPWIRE intents looped through camaro at 5/tick.
                #
                # Fix: stamp the pipeline's terminal verdict back onto
                # `shared_intents.gate_state` so the next tick skips
                # it via the existing `gate_state $nin [blocked,
                # no_trade, advisory_only]` filter on line 239.
                verdict = (r.get("verdict") or "").lower()
                terminal_state = None
                if verdict in ("no_trade", "blocked"):
                    terminal_state = "blocked"
                elif verdict == "advisory_only":
                    terminal_state = "advisory_only"
                elif verdict == "error":
                    # Broker / pipeline error — DON'T terminally
                    # block. Pod restarts / broker reconnections
                    # should let the intent retry on the next tick.
                    terminal_state = None
                if terminal_state:
                    try:
                        await db[SHARED_INTENTS].update_one(
                            {"intent_id": intent.get("intent_id")},
                            {"$set": {
                                "gate_state": terminal_state,
                                "last_submit_ts": _now_iso(),
                                "last_submit_by": AUTO_ROUTER_EMAIL,
                                "last_pipeline_verdict": verdict,
                                "last_pipeline_reason": r.get("reason"),
                            }},
                        )
                    except Exception as upd_err:  # noqa: BLE001
                        # Never let the audit-write failure crash
                        # the tick — better to re-process the intent
                        # next time than to nuke the whole loop.
                        logger.warning(
                            "auto-router terminal state writeback failed intent=%s err=%s",
                            intent.get("intent_id"), upd_err,
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
