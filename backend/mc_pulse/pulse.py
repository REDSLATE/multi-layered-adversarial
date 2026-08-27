"""The pulse loop — critical path only.

Design freeze: `MC_PULSE.md` §4. Non-critical maintenance (grader,
rollups, cleanup) belongs to a separate worker with its own
failure envelope.

The idempotency contract lives here + in the unique indexes:
    * A restarted pulse REUSES the same `pulse_id` on retry —
      writes upsert against (pulse_id, brain_id, symbol, lane)
      and (pulse_id, seat_key) so no double-execution.
    * `begin_pulse` allocates the id + persists a start marker.
    * `complete_pulse` finalizes the receipt.
    * Crash between the two → the start marker is visible; a
      later reconciler can decide to close it as `overrun=True`.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Iterable

from db import db
from shared.retention import ttl_stamp
from shared.wave_intelligence import summarize_wave_observations
from mc_arbiter.arbiter import MC_SEATS
from mc_arbiter.seat_key import build_seat_key
from mc_brains.camino import CaminoBrain, CaminoManifestHint
from mc_pulse.containment import evaluate_brain
from mc_pulse.envelope import OpinionEnvelope
from mc_pulse.input_manifest import (
    build_camino_manifest,
    persist_manifest,
)
from mc_pulse.protocols import Brain
from mc_pulse.receipt import BrainFailure, BrainSilence, PulseReceipt, persist_receipt
from mc_pulse.registry import get_registry
from mc_pulse.snapshot import MarketSnapshot

logger = logging.getLogger("mc_pulse.pulse")


async def begin_pulse(cadence_seconds: int) -> PulseReceipt:
    """Allocate a fresh pulse_id and persist a start marker so a
    crashed pulse is still visible in `mc_pulses`."""
    receipt = PulseReceipt(
        pulse_id=uuid.uuid4().hex,
        started_at=_now_iso(),
        cadence_seconds=cadence_seconds,
    )
    await persist_receipt(receipt)
    return receipt


async def complete_pulse(receipt: PulseReceipt) -> PulseReceipt:
    """Finalize the receipt. `overrun` is computed here — if the
    pulse took longer than the cadence, mark it so `orchestration_ok`
    returns False."""
    receipt.completed_at = _now_iso()
    if receipt.cadence_seconds > 0:
        started = datetime.fromisoformat(receipt.started_at)
        completed = datetime.fromisoformat(receipt.completed_at)
        elapsed = (completed - started).total_seconds()
        receipt.overrun = elapsed > receipt.cadence_seconds
    # Emission-health counters (2026-07-27): brain flow vs doctrine
    # flow must be independently observable.
    try:
        from shared.observability.pipeline_counters import incr  # noqa: WPS433
        evaluated = len(receipt.brains_completed)
        with_opinions = len(receipt.opinions_by_brain)
        incr("brains_evaluated", evaluated)
        incr("brain_holds", max(0, evaluated - with_opinions))
        incr("actionable_opinions", sum(receipt.opinions_by_brain.values()))
        incr("intents_emitted", receipt.intents_emitted)
    except Exception:  # noqa: BLE001
        pass
    await persist_receipt(receipt)
    return receipt


async def pulse_tick(
    snapshots: Iterable[MarketSnapshot],
    *,
    cadence_seconds: int = 15,
    runtime_mode: str = "DISARMED",
    compare_only: bool = True,
    auto_arbitrate: bool = False,
) -> PulseReceipt:
    """One critical-path pulse.

    Args:
        snapshots: pre-built immutable snapshots, one per
                   (lane, symbol) currently in the universe.
        cadence_seconds: informs `overrun` calc. If a pulse takes
                         longer than this, it's marked orchestration-
                         unhealthy.
        runtime_mode: DISARMED (default) or LIVE. Passed to the
                      arbiter when we route this pulse's envelopes.
        compare_only: TRUE during migration steps 2–5. Writes
                      envelopes to `mc_opinions_compare` INSTEAD
                      of `mc_seats`. Nothing gets arbitrated,
                      nothing reaches the trader. FALSE only
                      after all four brains have passed parity.
        auto_arbitrate: When True AND `compare_only=False`, arbitrate
                        every seat_key touched this tick immediately
                        after upserting envelopes to `mc_seats`. The
                        arbiter receives `runtime_mode` and — when
                        LIVE — emits an intent per winning seat via
                        `shared.intents._post_intent_impl`. This is
                        the closing link that connects the pulse
                        pipeline to the trader. Fail-soft per seat:
                        one bad arbitration never nukes the pulse.

    Returns:
        A completed `PulseReceipt` with per-brain outcomes.
    """
    receipt = await begin_pulse(cadence_seconds)
    try:
        return await _pulse_tick_impl(
            receipt,
            snapshots,
            cadence_seconds=cadence_seconds,
            runtime_mode=runtime_mode,
            compare_only=compare_only,
            auto_arbitrate=auto_arbitrate,
        )
    except Exception as exc:  # noqa: BLE001
        # 2026-07-15 (iter-30 P2): defensive belt on the pulse
        # orchestrator. Any exception that escapes brain-level
        # containment is a genuine orchestrator bug — stamp the
        # receipt with a compact `orchestration_error` string so
        # the operator health strip can point at THIS exception,
        # not just an empty red ⚠. We STILL persist the receipt
        # (so the tick is visible in the tape) and re-raise so the
        # worker can log the traceback for forensics.
        err_msg = f"{type(exc).__name__}: {str(exc)[:200]}"
        receipt.orchestration_error = err_msg
        logger.exception(
            "pulse orchestrator raised pulse_id=%s: %s",
            receipt.pulse_id, err_msg,
        )
        try:
            await complete_pulse(receipt)
        except Exception as persist_exc:  # noqa: BLE001
            logger.warning(
                "pulse orchestrator error-path receipt persist failed "
                "pulse_id=%s: %s",
                receipt.pulse_id, persist_exc,
            )
        raise


async def _pulse_tick_impl(
    receipt: PulseReceipt,
    snapshots: Iterable[MarketSnapshot],
    *,
    cadence_seconds: int,
    runtime_mode: str,
    compare_only: bool,
    auto_arbitrate: bool,
) -> PulseReceipt:
    """Extracted body of `pulse_tick`. Kept as a private helper so
    the outer `pulse_tick` can wrap it in a single top-level
    exception handler without indenting the whole file."""
    registry = get_registry()
    now = datetime.now(timezone.utc)

    snapshots = list(snapshots)
    receipt.snapshot_count = len(snapshots)
    receipt.brains_expected = len(registry)
    receipt.runtime_mode = runtime_mode
    receipt.wave_machine_summary = summarize_wave_observations(
        dict(snap.wave_intelligence)
        for snap in snapshots
        if snap.wave_intelligence
    )

    # Fan out: every (brain, snapshot) pair where the brain wants
    # a shot at this snapshot AND trades this lane.
    #
    # 2026-07 iter-27 doctrine gate: a snapshot with
    # `health.status != "fresh"` MUST NOT reach a brain. This is
    # the explicit contract fix for the 2026-07-11 incident where
    # a stale feeder produced 472 identical Camino/NVDA intents
    # from a 20-hour-old bar. A stale-input evaluation is an
    # OPERATIONAL ABSTENTION (no opinion), never a market
    # opinion. Stale-skipped events don't touch personality
    # stats, consensus, parity metrics, or execution. See
    # `mc_pulse.freshness` for the health contract.
    tasks = []
    task_meta: list[tuple[Brain, MarketSnapshot, str]] = []
    stale_skipped = 0
    for snap in snapshots:
        if snap.health is not None and not snap.health.is_fresh:
            stale_skipped += 1
            # P1 (2026-02-11): stamp each brain that WOULD have
            # evaluated this snapshot with a `snapshot_stale`
            # silence. Otherwise the health tile can't tell an
            # operator whether the 61% no-data was "market
            # closed" or "brain cooldown" or "no bars ever".
            for brain in registry.for_lane(snap.lane):
                receipt.brains_silent.append(BrainSilence(
                    brain_id=brain.id,
                    reason="snapshot_stale",
                    symbol=snap.symbol,
                    lane=snap.lane,
                ))
            continue
        for brain in registry.for_lane(snap.lane):
            if not brain.should_evaluate(now=now, snapshot=snap):
                # P1: stamp cadence-cooldown silence.
                receipt.brains_silent.append(BrainSilence(
                    brain_id=brain.id,
                    reason="cadence_cooldown",
                    symbol=snap.symbol,
                    lane=snap.lane,
                ))
                continue
            seat_key = build_seat_key(snap.lane, snap.symbol, now)
            tasks.append(evaluate_brain(brain, snap, receipt.pulse_id, seat_key=seat_key))
            task_meta.append((brain, snap, seat_key))
    if stale_skipped:
        logger.info(
            "pulse tick pulse_id=%s stale_snapshots_skipped=%d",
            receipt.pulse_id, stale_skipped,
        )

    if not tasks:
        # Nothing to do this tick — still emit a receipt so the
        # heartbeat clock advances. Design freeze §7: pulse
        # receipt IS the heartbeat.
        return await complete_pulse(receipt)

    # Note: return_exceptions=False here means "any raise from
    # `evaluate_brain` itself is an orchestration bug." That
    # function ALREADY catches brain-level failures — so any
    # exception reaching here is our bug, not a brain's.
    results = await asyncio.gather(*tasks, return_exceptions=False)

    envelopes: list[OpinionEnvelope] = []
    brains_completed: set[str] = set()
    for (brain, _snap, _seat), (env, fail) in zip(task_meta, results):
        if env is not None:
            envelopes.append(env)
            brains_completed.add(brain.id)
        elif fail is not None:
            receipt.brains_failed.append(fail)
        else:
            # Brain returned None — completed cleanly, just had
            # nothing to say. Counts as completed AND stamps a
            # `no_signal_return` silence so the health tile can
            # show it alongside the other no-data reasons.
            brains_completed.add(brain.id)
            receipt.brains_silent.append(BrainSilence(
                brain_id=brain.id,
                reason="no_signal_return",
                symbol=_snap.symbol,
                lane=_snap.lane,
            ))
    receipt.brains_completed = sorted(brains_completed)
    for env in envelopes:
        receipt.opinions_by_brain[env.brain_id] = (
            receipt.opinions_by_brain.get(env.brain_id, 0) + 1
        )

    # ── Manifest persistence (2026-07 parity step 4) ──
    # After every brain has evaluated, drain hints from any brain
    # that exposes `take_manifest_hint`. The orchestrator writes
    # the manifest; the brain never persists. Fail-soft — a
    # broken manifest write must not take down the pulse.
    await _persist_hints(task_meta)

    # Persist envelopes. During migration `compare_only=True` sends
    # them to a separate collection so we can inspect parity
    # without disturbing the live arbiter tape.
    persistence_target = (
        "mc_opinions_compare" if compare_only else MC_SEATS
    )
    await _upsert_envelopes(envelopes, persistence_target)

    # ── Auto-arbitration (2026-07-13 P0 fix) ───────────────────
    # Historically the arbiter was only ever invoked via the HTTP
    # `/api/mc/arbiter/arbitrate/{seat_key}` endpoint or from the
    # E2E trace tool — so pulse-emitted opinions accumulated on
    # `mc_seats` but no winner was ever picked and no intent
    # reached the trader. The trader silence was the consequence.
    #
    # When `auto_arbitrate=True` AND `compare_only=False`, we
    # arbitrate every unique seat_key touched by THIS pulse. If
    # `runtime_mode == "LIVE"` the arbiter emits a real intent
    # into `shared_intents`; if DISARMED the decision is still
    # recorded (DAWE grading path stays intact) but no intent is
    # emitted. Fail-soft per seat — one bad arbitration never
    # nukes the pulse tick.
    if auto_arbitrate and not compare_only and envelopes:
        from mc_arbiter.arbiter import arbitrate  # noqa: WPS433
        from mc_arbiter.models import RuntimeMode  # noqa: WPS433
        try:
            mode = RuntimeMode(runtime_mode)
        except ValueError:
            mode = RuntimeMode.DISARMED
        seat_keys = sorted({e.seat_key for e in envelopes if e.seat_key})
        outcomes = receipt.arbitration_outcomes
        for seat_key in seat_keys:
            try:
                decision = await arbitrate(seat_key, runtime_mode=mode)
                receipt.arbitrations_completed += 1
                if decision.get("intent_id"):
                    receipt.intents_emitted += 1
                    outcomes["emitted"] = outcomes.get("emitted", 0) + 1
                elif decision.get("suppressed_duplicate"):
                    outcomes["suppressed_duplicate"] = (
                        outcomes.get("suppressed_duplicate", 0) + 1
                    )
                elif decision.get("emit_error"):
                    outcomes["emit_error"] = outcomes.get("emit_error", 0) + 1
                elif decision.get("winner_brain") and mode != RuntimeMode.LIVE:
                    outcomes["suppressed_disarmed"] = (
                        outcomes.get("suppressed_disarmed", 0) + 1
                    )
                else:
                    reason = decision.get("reason") or "no_winner"
                    outcomes[reason] = outcomes.get(reason, 0) + 1
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "auto_arbitrate failed pulse_id=%s seat=%s err=%s",
                    receipt.pulse_id, seat_key, exc,
                )

    return await complete_pulse(receipt)


async def _upsert_envelopes(
    envelopes: list[OpinionEnvelope], collection_name: str,
) -> None:
    """Upsert by the enforced-unique key `(seat_key, brain)` — the
    same index the `mc_seats_seat_brain` unique constraint pins
    (see `db.ensure_indexes`).

    2026-07-14 (iter-30): switched the filter from `(pulse_id,
    brain, symbol, lane)` to `(seat_key, brain)`. The seat_key
    already encodes `{lane}:{symbol}:{5-min-bucket}`, and the
    doctrine (see index doc string) is "one row per (seat_key,
    brain) so all N brains competing for the same 5-min bucket
    surface with a single seat_key lookup." Multiple pulse ticks
    that fire inside the same 5-min bucket all resolve to the
    same seat_key + brain → the second tick should UPDATE the
    row, not INSERT a new one. The old `pulse_id`-scoped filter
    inserted a fresh doc per pulse, which then collided with the
    (seat_key, brain) unique index — the log's E11000 flood.
    """
    if not envelopes:
        return
    # 2026-07-15 iter-30 P4c: bound each individual upsert with an
    # asyncio timeout so a slow Atlas can't hang the pulse worker
    # for 9+ minutes doing sequential writes. Without this, 50
    # symbols × 4 brains = 200 sequential upserts with no ceiling —
    # any degraded-Atlas moment cascades into pulse overrun. With
    # the 2s cap, a slow write raises `TimeoutError`, gets logged
    # as a per-upsert warning, and the pulse continues (missing
    # one seat's opinion for that tick is much better than the
    # whole pulse being 9× overrun).
    for env in envelopes:
        doc = env.to_mongo()
        doc["ttl_at"] = ttl_stamp()
        try:
            await asyncio.wait_for(
                db[collection_name].update_one(
                    {
                        "seat_key": env.seat_key,
                        "brain": env.brain_id,
                    },
                    {
                        "$set": doc,
                        "$setOnInsert": {"first_recorded_at": _now_iso()},
                    },
                    upsert=True,
                ),
                timeout=2.0,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "envelope upsert TIMED OUT pulse=%s brain=%s symbol=%s "
                "(atlas slow? skipping this seat for this tick)",
                env.pulse_id, env.brain_id, doc.get("symbol"),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "envelope upsert failed pulse=%s brain=%s symbol=%s err=%s",
                env.pulse_id, env.brain_id, doc.get("symbol"), exc,
            )


async def _persist_hints(
    task_meta: list[tuple[Brain, MarketSnapshot, str]],
) -> None:
    """Drain per-brain manifest hints and persist them.

    2026-07 parity step 4. Persistence lives HERE, not on the
    brain — the brain records diagnostic material inside
    `evaluate` and the orchestrator writes it after evaluation.
    This preserves the "brains do interpretation only" boundary
    while still capturing the same manifest schema on both the
    pulse and the runner side.

    Currently Camino-specific because Camino is the pilot
    migration. When other brains land, either (a) each ships its
    own `build_manifest_from_hint` on the brain type and this
    function dispatches by type, or (b) we formalize a Brain
    protocol method `Brain.persist_manifest(orchestrator_ctx)`.
    Deferring the abstraction until we have two concrete
    implementations — one is not a pattern.

    Fail-soft: a broken manifest write must NEVER take down the
    pulse tick. Every write is wrapped by `persist_manifest`.
    """
    seen: set[tuple[str, str]] = set()
    for brain, snap, _seat in task_meta:
        if not isinstance(brain, CaminoBrain):
            continue
        # Drain the hint even if we can't build a manifest — pop
        # semantics avoids stale hints leaking into a later tick.
        hint = brain.take_manifest_hint(snap.symbol)
        if hint is None:
            continue
        if snap.bar_identity is None:
            # No canonical bar → no parity join. Skip silently;
            # the parity endpoint will report the pair as
            # `parity_key_missing`.
            continue
        try:
            parity_key = snap.bar_identity.to_parity_key(
                brain_id=brain.id, symbol=snap.symbol,
            )
        except ValueError as exc:
            logger.warning(
                "manifest parity key composition failed "
                "brain=%s symbol=%s err=%s",
                brain.id, snap.symbol, exc,
            )
            continue
        dedup_key = (parity_key.as_string(), "pulse")
        if dedup_key in seen:
            # Same (brain, symbol, bar) reached us twice this
            # tick — that's an orchestration bug, but we'd rather
            # log-and-continue than write twice.
            logger.warning(
                "duplicate manifest hint dropped brain=%s symbol=%s",
                brain.id, snap.symbol,
            )
            continue
        seen.add(dedup_key)
        manifest = build_camino_manifest(
            parity_key=parity_key,
            path="pulse",
            bar=snap.bar_identity,
            source_bar_id=snap.source_bar_id,
            snapshot=hint.feature_snapshot,
            fallback_used=hint.fallback_used,
            position_context_present=hint.position_context_present,
            bar_count=hint.bar_count,
            action=hint.action,
            confidence=hint.confidence,
            status=hint.status,
            reason_codes=hint.reason_codes,
        )
        await persist_manifest(manifest)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
