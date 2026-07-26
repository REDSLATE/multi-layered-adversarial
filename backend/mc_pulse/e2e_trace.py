"""E2E execution trace — controlled diagnostic that drives one
synthesized intent through every layer of the stack and verifies
each expected DB write lands. Designed as BOTH:

  1. **Diagnostic runner** — operator can call `run_e2e_trace(...)`
     from a shell / admin endpoint to answer "why isn't the council
     producing trades?" by seeing WHICH link in the chain broke.
  2. **Integration test** — `tests/test_e2e_execution_trace.py`
     runs the same trace against a mocked broker, asserting every
     expected write. Catches silent regressions in the pulse →
     opinion → arbiter → intent → router → broker chain.

Trace steps + the write each one MUST land:

    [1] synthesize snapshot   → returns MarketSnapshot
    [2] run_pulse(1 snap)     → ≥1 opinion in mc_seats
    [3] arbitrate(seat_key)   → decision doc on winner's mc_seats row
    [4] emit intent           → row in shared_intents with intent_id
    [5] route_one(intent)     → gate_state ∈ {submitted, blocked, error}
    [6] broker call           → broker_order.id present (submitted case)
    [7] executions.record     → row in shared_executions

Any missing write returns a `TraceResult` with `broke_at` pointing
at the last successful step + `next_expected` naming what should
have happened next. Operators + CI both benefit from that shape.

SAFETY:
  * The trace NEVER submits to a real broker. Callers MUST pass
    `broker_mock=True` (default) OR patch `broker_router.route_order`
    beforehand. A live-broker run is explicit opt-in and gated on
    an env var (`E2E_TRACE_ALLOW_LIVE_BROKER=1`) to prevent
    accidental order placement.
  * All test writes are cleaned up via a per-trace `trace_id` that
    tags every row it produces. `cleanup_trace(trace_id)` removes them.
"""
from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger("mc_pulse.e2e_trace")


# ── Result types ────────────────────────────────────────────────
STAGE_NAMES = [
    "synthesize_snapshot",
    "run_pulse_writes_opinion",
    "arbitrate_writes_decision",
    "emit_intent",
    "route_one",
    "broker_call",
    "executions_record",
]


@dataclass
class StageResult:
    """One step in the trace. `ok=True` means the expected DB write
    landed; `detail` carries the observed shape (or the exception)."""
    name: str
    ok: bool
    detail: dict[str, Any] = field(default_factory=dict)
    duration_ms: float = 0.0
    error: Optional[str] = None


@dataclass
class TraceResult:
    trace_id: str
    started_at: str
    completed_at: str
    ok: bool
    stages: list[StageResult] = field(default_factory=list)
    broke_at: Optional[str] = None
    next_expected: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "trace_id": self.trace_id,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "ok": self.ok,
            "broke_at": self.broke_at,
            "next_expected": self.next_expected,
            "stages": [
                {
                    "name": s.name,
                    "ok": s.ok,
                    "duration_ms": s.duration_ms,
                    "detail": s.detail,
                    "error": s.error,
                } for s in self.stages
            ],
        }


# ── Trace core ──────────────────────────────────────────────────
async def run_e2e_trace(
    *,
    symbol: str = "AAPL",
    lane: str = "equity",
    broker_mock: bool = True,
    cleanup: bool = True,
) -> TraceResult:
    """Drive one synthesized intent through the whole stack.

    Args:
        symbol: symbol to trace. Defaults to AAPL because it has
            reliable market data.
        lane: `equity` or `crypto`. Determines snapshot health +
            broker path.
        broker_mock: when True, replaces `broker_router.route_order`
            with an in-memory stub that returns a synthetic order
            receipt. When False, uses the real broker — requires
            `E2E_TRACE_ALLOW_LIVE_BROKER=1` in the environment.
        cleanup: when True, removes all rows tagged with the trace
            ID at the end (default; keeps preview + prod DBs clean).

    Returns:
        `TraceResult` — inspect `stages` to see which link broke.
    """
    from datetime import datetime, timezone
    if not broker_mock:
        if os.environ.get("E2E_TRACE_ALLOW_LIVE_BROKER") != "1":
            raise RuntimeError(
                "e2e trace refused: broker_mock=False requires "
                "E2E_TRACE_ALLOW_LIVE_BROKER=1 (safety guard).",
            )

    trace_id = f"e2e-{uuid.uuid4().hex[:12]}"
    started = datetime.now(timezone.utc).isoformat()
    result = TraceResult(
        trace_id=trace_id, started_at=started, completed_at="", ok=False,
    )

    async def _run_stage(name: str, fn) -> Optional[Any]:
        """Wrap a stage: catches exceptions, times it, appends to
        result.stages. Returns the fn's return value on success or
        None on failure. Caller MUST check `stages[-1].ok`."""
        import time
        t0 = time.perf_counter()
        try:
            payload = await fn()
            duration_ms = (time.perf_counter() - t0) * 1000
            result.stages.append(StageResult(
                name=name, ok=True, detail=payload or {},
                duration_ms=round(duration_ms, 2),
            ))
            return payload
        except Exception as exc:  # noqa: BLE001
            duration_ms = (time.perf_counter() - t0) * 1000
            result.stages.append(StageResult(
                name=name, ok=False, duration_ms=round(duration_ms, 2),
                error=f"{type(exc).__name__}: {str(exc)[:500]}",
            ))
            return None

    # Wire broker mock BEFORE any stage runs.
    broker_patch = None
    if broker_mock:
        broker_patch = _install_broker_mock(trace_id)

    try:
        # ── Stage 1: synthesize snapshot ───────────────────────────
        snap_result = await _run_stage(
            "synthesize_snapshot",
            lambda: _stage_synth_snapshot(symbol, lane, trace_id),
        )
        if snap_result is None:
            return _finalize(result, broke_at="synthesize_snapshot",
                             next_expected="run_pulse_writes_opinion",
                             cleanup=cleanup, trace_id=trace_id,
                             broker_patch=broker_patch)
        # Pull out the snapshot obj so downstream stages can use it,
        # then strip it from the detail (not JSON-serialisable —
        # frozen MappingProxyType breaks the FastAPI encoder).
        snap = snap_result.pop("snapshot_obj", None)
        result.stages[-1].detail = snap_result

        # ── Stage 2: run pulse ─────────────────────────────────────
        pulse_out = await _run_stage(
            "run_pulse_writes_opinion",
            lambda: _stage_run_pulse(snap, trace_id),
        )
        if pulse_out is None:
            return _finalize(result, broke_at="synthesize_snapshot",
                             next_expected="run_pulse_writes_opinion",
                             cleanup=cleanup, trace_id=trace_id,
                             broker_patch=broker_patch)
        seat_key = pulse_out.get("seat_key")
        if not seat_key:
            return _finalize(result, broke_at="run_pulse_writes_opinion",
                             next_expected="arbitrate_writes_decision",
                             cleanup=cleanup, trace_id=trace_id,
                             broker_patch=broker_patch)

        # ── Stage 3: arbitrate ─────────────────────────────────────
        arb = await _run_stage(
            "arbitrate_writes_decision",
            lambda: _stage_arbitrate(seat_key),
        )
        if arb is None:
            return _finalize(result, broke_at="run_pulse_writes_opinion",
                             next_expected="arbitrate_writes_decision",
                             cleanup=cleanup, trace_id=trace_id,
                             broker_patch=broker_patch)

        intent_id = arb.get("intent_id")
        # ── Stage 4: emit_intent (verify shared_intents row) ───────
        emit = await _run_stage(
            "emit_intent",
            lambda: _stage_emit(intent_id, arb),
        )
        if emit is None or not emit.get("intent_row"):
            return _finalize(result, broke_at="arbitrate_writes_decision",
                             next_expected="emit_intent",
                             cleanup=cleanup, trace_id=trace_id,
                             broker_patch=broker_patch)

        # ── Stage 5: route_one ─────────────────────────────────────
        route_out = await _run_stage(
            "route_one",
            lambda: _stage_route(emit["intent_row"]),
        )
        if route_out is None:
            return _finalize(result, broke_at="emit_intent",
                             next_expected="route_one",
                             cleanup=cleanup, trace_id=trace_id,
                             broker_patch=broker_patch)

        # ── Stage 6: broker call (verify broker_order.id) ──────────
        broker_out = await _run_stage(
            "broker_call",
            lambda: _stage_verify_broker(intent_id),
        )
        if broker_out is None:
            return _finalize(result, broke_at="route_one",
                             next_expected="broker_call",
                             cleanup=cleanup, trace_id=trace_id,
                             broker_patch=broker_patch)

        # ── Stage 7: executions.record ─────────────────────────────
        exec_out = await _run_stage(
            "executions_record",
            lambda: _stage_verify_execution(intent_id),
        )
        if exec_out is None:
            return _finalize(result, broke_at="broker_call",
                             next_expected="executions_record",
                             cleanup=cleanup, trace_id=trace_id,
                             broker_patch=broker_patch)

        result.ok = True
        return _finalize(result, broke_at=None, next_expected=None,
                         cleanup=cleanup, trace_id=trace_id,
                         broker_patch=broker_patch)
    finally:
        pass  # _finalize handles teardown


def _finalize(
    result: TraceResult, *, broke_at: Optional[str],
    next_expected: Optional[str], cleanup: bool,
    trace_id: str, broker_patch: Any,
) -> TraceResult:
    result.completed_at = datetime.now(timezone.utc).isoformat()
    result.broke_at = broke_at
    result.next_expected = next_expected
    if broker_patch is not None:
        broker_patch.stop()
    # Cleanup is best-effort — never let it mutate the trace verdict.
    if cleanup:
        try:
            import asyncio
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # Schedule cleanup as fire-and-forget; the trace has
                # already been returned to the caller.
                asyncio.create_task(cleanup_trace(trace_id))
            else:
                loop.run_until_complete(cleanup_trace(trace_id))
        except Exception as exc:  # noqa: BLE001
            logger.warning("e2e trace cleanup scheduling failed: %s", exc)
    return result


# ── Broker mock ─────────────────────────────────────────────────
class _BrokerMock:
    """Monkey-patch handle for the broker + master-switch layers.

    Two patches:
      1. `shared.broker_router.route_order` — replaced with an
         in-memory synthetic receipt tagged with trace_id.
      2. `shared.auto_router._is_master_switch_armed` — forced to
         return True so the router gate doesn't block on the
         operator's disarmed state. Safer than flipping the DB
         flag because a crash can't leave the master switch armed.

    Both patches revert on `stop()`.
    """
    def __init__(self, trace_id: str):
        self.trace_id = trace_id
        self._original_route_order = None
        self._original_arm_check = None
        self._original_is_rth = None
        self._original_is_ext = None

    def start(self):
        from shared import auto_router, broker_router
        self._original_route_order = broker_router.route_order
        self._original_arm_check = auto_router._is_master_switch_armed

        async def _fake_route_order(intent, *, notional_usd, client_order_id):
            # Return the same shape the real broker does, minus real
            # side effects. Include the trace_id so cleanup can find
            # any leftover rows.
            return {
                "id": f"mock-{uuid.uuid4().hex[:16]}",
                "order_id": f"mock-{uuid.uuid4().hex[:16]}",
                "broker": "e2e_mock",
                "broker_symbol": intent.get("symbol"),
                "canonical": intent.get("symbol"),
                "lane": intent.get("lane"),
                "side": intent.get("action"),
                "qty": max(1, int(notional_usd / 100)),
                "notional": notional_usd,
                "status": "submitted",
                "filled_qty": 0,
                "filled_avg_price": None,
                "submitted_at": datetime.now(timezone.utc).isoformat(),
                "e2e_trace_id": self.trace_id,
            }

        async def _fake_arm_check() -> bool:
            return True

        broker_router.route_order = _fake_route_order
        auto_router._is_master_switch_armed = _fake_arm_check

        # ── Market-hours patch (equity lane only) ──
        # The router's Stage 3 has `is_equity_rth() / is_equity_
        # extended_hours()` gates that block submits outside RTH.
        # Patch both to return True for the duration of the trace
        # so equity-lane traces can complete off-hours (nights /
        # weekends). Crypto lane is unaffected — it has no
        # market-hours gate.
        from shared import market_hours as mh
        self._original_is_rth = mh.is_equity_rth
        self._original_is_ext = mh.is_equity_extended_hours
        mh.is_equity_rth = lambda: True
        mh.is_equity_extended_hours = lambda: True

        # Bust any cached arm state so the fake takes effect
        # immediately.
        try:
            auto_router._invalidate_arm_cache()
        except Exception:  # noqa: BLE001
            pass

    def stop(self):
        from shared import auto_router, broker_router
        if self._original_route_order is not None:
            broker_router.route_order = self._original_route_order
            self._original_route_order = None
        if self._original_arm_check is not None:
            auto_router._is_master_switch_armed = self._original_arm_check
            self._original_arm_check = None
        # Restore market-hours funcs.
        try:
            from shared import market_hours as mh
            if self._original_is_rth is not None:
                mh.is_equity_rth = self._original_is_rth
                self._original_is_rth = None
            if self._original_is_ext is not None:
                mh.is_equity_extended_hours = self._original_is_ext
                self._original_is_ext = None
        except Exception:  # noqa: BLE001
            pass
        # Bust the cache again so the real check re-reads Mongo.
        try:
            from shared import auto_router as ar
            ar._invalidate_arm_cache()
        except Exception:  # noqa: BLE001
            pass


def _install_broker_mock(trace_id: str) -> _BrokerMock:
    m = _BrokerMock(trace_id)
    m.start()
    return m


# ── Stage implementations ───────────────────────────────────────
async def _stage_synth_snapshot(symbol: str, lane: str, trace_id: str) -> dict:
    """Build one MarketSnapshot with a FRESH health status. Uses the
    canonical `build_snapshot` construction path so any breakage in
    the snapshot API would surface here rather than being hidden."""
    from decimal import Decimal

    from mc_pulse.freshness import SnapshotHealth
    from mc_pulse.snapshot import build_snapshot

    now = datetime.now(timezone.utc)
    health = SnapshotHealth(
        status="fresh",
        latest_bar_at=now,
        age_seconds=10.0,
        max_age_seconds=300.0,
    )
    # Rich enough indicator + feature payload for at least one brain
    # to produce a directional stance. The trend/momentum brains read
    # `feature_snapshot`; execution/mean-reversion also look at
    # indicators. We synthesise an unambiguously bullish setup
    # (trend > 0.7, price confirmation > 0.08, extended RSI) so
    # multiple brains have a valid case for LONG.
    indicators = {
        "close": 150.00,
        "ema_20": 148.50,
        "ema_50": 147.00,
        "rsi_14": 62.0,
        "atr_14": 2.10,
        "vwap": 149.50,
        "spread_bps": 2.5,
        # Trace tag — lets cleanup find derived rows.
        "trace_id": trace_id,
    }
    feature_snapshot = {
        # Camino trend brain
        "trend_score": 0.85,
        "price_change_pct": 0.12,
        "market_regime": "trending",
        # GTO momentum brain
        "momentum_score": 0.75,
        "momentum_confirmation_count": 3,
        # Barracuda mean-reversion (should decline — not overbought
        # enough to fade a strong trend)
        "z_score": 0.4,
        "band_position": 0.3,
        # Hellcat execution safety
        "spread_bps": 2.5,
        "liquidity_score": 0.9,
        "microstructure_ok": True,
        "trace_id": trace_id,
    }
    snap = build_snapshot(
        symbol=symbol.upper(),
        lane=lane.lower(),
        timestamp=now,
        price=Decimal("150.00"),
        indicators=indicators,
        source_tf="1m",
        source_bar_count=120,
    )
    # `build_snapshot` doesn't take health or feature_snapshot as
    # kwargs — attach both via replace() since MarketSnapshot is
    # frozen. feature_snapshot MUST be MappingProxyType (matches
    # the frozen contract).
    from dataclasses import replace
    from types import MappingProxyType
    snap = replace(
        snap,
        health=health,
        feature_snapshot=MappingProxyType(feature_snapshot),
        market_state="trending",
    )
    return {"snapshot_obj": snap, "symbol": symbol, "lane": lane}


async def _stage_run_pulse(snap, trace_id: str) -> dict:
    """Drive `pulse_tick([snap])` and verify at least one opinion
    landed in `mc_seats` for this snapshot's seat_key. Returns the
    seat_key so downstream stages can arbitrate on it."""
    from db import db
    from mc_arbiter.seat_key import build_seat_key
    from mc_pulse.pulse import pulse_tick
    from mc_pulse.registry import get_registry

    # Ensure brains are registered. In the server-boot path this
    # happens via `server_modules.lifespan`; when the trace runs
    # from a shell / pytest / diagnostic endpoint the registry may
    # be empty. Idempotent — no-op if already registered.
    registry = get_registry()
    if len(registry) == 0:
        from mc_brains.barracuda import BarracudaBrain
        from mc_brains.camino import CaminoBrain
        from mc_brains.gto import GtoBrain
        from mc_brains.hellcat import HellcatBrain
        for brain_cls in (CaminoBrain, GtoBrain, BarracudaBrain, HellcatBrain):
            inst = brain_cls()
            if inst.id not in registry.ids():
                registry.register(inst)

    now = datetime.now(timezone.utc)
    seat_key = build_seat_key(snap.lane, snap.symbol, now)
    # `compare_only=False` writes to mc_seats (the arbitration path).
    # `compare_only=True` writes to mc_opinions_compare (migration
    # sidecar) and no arbitration happens. We need False here.
    receipt = await pulse_tick(
        snapshots=[snap],
        cadence_seconds=15,
        runtime_mode="LIVE",
        compare_only=False,
    )
    # Confirm at least one opinion has this seat_key.
    n_opinions = await db["mc_seats"].count_documents({"seat_key": seat_key})
    if n_opinions == 0:
        raise RuntimeError(
            f"run_pulse returned pulse_id={receipt.pulse_id} but no "
            f"opinions landed at seat_key={seat_key}. "
            f"brains_completed={receipt.brains_completed} "
            f"brains_failed={len(receipt.brains_failed)} "
            f"brains_silent_count={len(receipt.brains_silent)}",
        )
    return {
        "pulse_id": receipt.pulse_id,
        "seat_key": seat_key,
        "opinion_count": n_opinions,
        "brains_completed": receipt.brains_completed,
    }


async def _stage_arbitrate(seat_key: str) -> dict:
    """Call `arbitrate(seat_key, LIVE)` and verify the decision
    doc landed on the winner's mc_seats row. Also captures the
    `intent_id` if one was emitted."""
    from db import db
    from mc_arbiter.arbiter import arbitrate
    from mc_arbiter.models import RuntimeMode

    decision = await arbitrate(seat_key, runtime_mode=RuntimeMode.LIVE)
    if not decision:
        raise RuntimeError("arbitrate returned empty decision")
    # Confirm the decision landed on the seat tape.
    winner_brain = decision.get("winner_brain")
    if winner_brain:
        stamped = await db["mc_seats"].find_one({
            "seat_key": seat_key, "brain": winner_brain,
            "decision": {"$exists": True},
        }, {"decision.intent_id": 1, "decision.winner_brain": 1})
        if not stamped:
            raise RuntimeError(
                f"arbitrate produced decision (winner={winner_brain}) "
                f"but the stamp did NOT land on mc_seats.",
            )
    return {
        "winner_brain": winner_brain,
        "winner_direction": decision.get("winner_direction"),
        "intent_id": decision.get("intent_id"),
        "emit_error": decision.get("emit_error"),
        "no_decision": decision.get("no_decision"),
        "reason": decision.get("reason"),
    }


async def _stage_emit(intent_id: Optional[str], arb: dict) -> dict:
    """Verify a `shared_intents` row exists for the intent_id
    emitted by arbitrate. If arbitrate returned `no_decision` or an
    `emit_error`, propagate a clear message — the stack works but
    the arbiter decided not to trade."""
    from db import db
    if not intent_id:
        reason = (
            arb.get("emit_error") or arb.get("reason")
            or "arbitrate returned no intent_id (no_decision or all_flat)"
        )
        raise RuntimeError(f"no intent_id emitted: {reason}")
    row = await db["shared_intents"].find_one(
        {"intent_id": intent_id}, {"_id": 0},
    )
    if not row:
        raise RuntimeError(
            f"intent_id={intent_id} not found in shared_intents",
        )
    return {"intent_id": intent_id, "intent_row": row}


async def _stage_route(intent_row: dict) -> dict:
    """Push the intent through `auto_router._route_one` and record
    the final gate_state. `submitted` = broker call happened;
    `blocked`/`error` = router refused. Either is a legitimate
    outcome from the operator's perspective — but the trace
    surfaces WHICH so they can diagnose."""
    from shared.auto_router import _route_one
    verdict = await _route_one(intent_row)
    return {
        "verdict": verdict.get("verdict"),
        "reason": verdict.get("reason"),
        "broker": verdict.get("broker"),
        "order_id": verdict.get("order_id"),
    }


async def _stage_verify_broker(intent_id: str) -> dict:
    """Verify the intent doc now has `broker_order.id` persisted —
    proof the broker call round-tripped."""
    from db import db
    row = await db["shared_intents"].find_one(
        {"intent_id": intent_id},
        {"_id": 0, "broker_order": 1, "gate_state": 1, "broker_reason": 1},
    )
    if not row:
        raise RuntimeError(
            f"intent {intent_id} vanished after route_one",
        )
    gate_state = row.get("gate_state")
    if gate_state != "submitted":
        # Known race: intents.py fires force_one_tick() on insert, so
        # the scheduled tick can beat the trace's own route stage —
        # the loser is blocked `already_executed_concurrent` and its
        # blocked stamp can even land AFTER the winner's submitted
        # stamp. The `executions` row (ok=True) is the system of
        # record — accept it as proof the broker call round-tripped.
        exec_row = await db["executions"].find_one(
            {"intent_id": intent_id, "risk_reason": "already_executed_concurrent"},
            {"_id": 0, "risk_reason": 1},
        )
        if exec_row is not None:
            import asyncio as _aio
            for _ in range(15):
                ok_row = await db["executions"].find_one(
                    {"intent_id": intent_id, "ok": True},
                    {"_id": 0, "broker": 1, "broker_order_id": 1,
                     "broker_status": 1},
                )
                if ok_row:
                    return {
                        "broker": ok_row.get("broker"),
                        "broker_order_id": ok_row.get("broker_order_id"),
                        "status": ok_row.get("broker_status"),
                    }
                row = await db["shared_intents"].find_one(
                    {"intent_id": intent_id},
                    {"_id": 0, "broker_order": 1, "gate_state": 1,
                     "broker_reason": 1},
                ) or row
                gate_state = row.get("gate_state")
                if gate_state == "submitted":
                    break
                await _aio.sleep(0.2)
    if gate_state != "submitted":
        raise RuntimeError(
            f"intent {intent_id} ended at gate_state={gate_state} "
            f"(broker_reason={row.get('broker_reason')}); no broker "
            f"call was made.",
        )
    broker_order = row.get("broker_order") or {}
    if not broker_order.get("id") and not broker_order.get("order_id"):
        raise RuntimeError(
            f"intent {intent_id} gate_state=submitted but no "
            f"broker_order.id was persisted.",
        )
    return {
        "broker": broker_order.get("broker"),
        "broker_order_id": broker_order.get("id") or broker_order.get("order_id"),
        "status": broker_order.get("status"),
    }


async def _stage_verify_execution(intent_id: str) -> dict:
    """Verify the `executions` row landed. This is the system of
    record — its presence proves the router closed the loop. If the
    broker call succeeded but this row is missing, the executions
    writer is broken.

    Two rows can legitimately exist for one intent: `intents.py`
    fires `force_one_tick()` on every insert, so the scheduled tick
    can race the trace's own route stage. The idempotency guard
    blocks the loser (`already_executed_concurrent`, ok=False) —
    exactly one broker submit. Prefer the ok=True row."""
    from db import db
    row = await db["executions"].find_one(
        {"intent_id": intent_id, "ok": True}, {"_id": 0},
    )
    if not row:
        row = await db["executions"].find_one(
            {"intent_id": intent_id}, {"_id": 0},
        )
    if not row:
        raise RuntimeError(
            f"no executions row for intent {intent_id}",
        )
    if not row.get("ok"):
        raise RuntimeError(
            f"execution row exists but ok=False "
            f"(risk_reason={row.get('risk_reason')})",
        )
    return {
        "ok": row.get("ok"),
        "broker": row.get("broker"),
        "broker_status": row.get("broker_status"),
        "notional_usd": row.get("notional_usd"),
    }


# ── Cleanup ─────────────────────────────────────────────────────
async def cleanup_trace(trace_id: str) -> dict:
    """Delete every row tagged with the trace_id. Used at the end
    of a trace + by pytest teardown fixtures."""
    from db import db
    stats = {}
    # Snapshot features carry trace_id; opinion sidecars do too.
    for coll, query in [
        ("mc_seats", {"snapshot.features.trace_id": trace_id}),
        ("mc_pulses", {"pulse_features_trace_id": trace_id}),
        ("shared_intents", {"evidence.e2e_trace_id": trace_id}),
        ("executions", {"e2e_trace_id": trace_id}),
        ("mc_brain_silences", {"e2e_trace_id": trace_id}),
    ]:
        try:
            r = await db[coll].delete_many(query)
            stats[coll] = r.deleted_count
        except Exception as exc:  # noqa: BLE001
            stats[coll] = f"err: {exc}"
    return stats
