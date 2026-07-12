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
    await persist_receipt(receipt)
    return receipt


async def pulse_tick(
    snapshots: Iterable[MarketSnapshot],
    *,
    cadence_seconds: int = 15,
    runtime_mode: str = "DISARMED",
    compare_only: bool = True,
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

    Returns:
        A completed `PulseReceipt` with per-brain outcomes.
    """
    receipt = await begin_pulse(cadence_seconds)
    registry = get_registry()
    now = datetime.now(timezone.utc)

    snapshots = list(snapshots)
    receipt.snapshot_count = len(snapshots)
    receipt.brains_expected = len(registry)
    receipt.runtime_mode = runtime_mode

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

    # Arbitration is DEFERRED until compare_only is False AND
    # migration step 6 has flipped the arbiter to read from
    # pulse-populated envelopes. Until then the arbiter continues
    # reading from runner-written rows in mc_seats. Design freeze
    # §14: everything from `mc_arbiter/` survives; pulse is
    # additive during the migration window.

    return await complete_pulse(receipt)


async def _upsert_envelopes(
    envelopes: list[OpinionEnvelope], collection_name: str,
) -> None:
    """Upsert by the composite idempotency key
    `(pulse_id, brain, symbol, lane)` — enforced at the schema
    level by the unique index in `db.ensure_indexes`.

    A retried pulse reuses `pulse_id`, so these upserts converge
    to at-most-one row per (pulse, brain, symbol, lane) even
    across crashes and retries.
    """
    if not envelopes:
        return
    for env in envelopes:
        doc = env.to_mongo()
        try:
            await db[collection_name].update_one(
                {
                    "pulse_id": env.pulse_id,
                    "brain": env.brain_id,
                    "symbol": doc["symbol"],
                    "lane": doc["lane"],
                },
                {
                    "$set": doc,
                    "$setOnInsert": {"first_recorded_at": _now_iso()},
                },
                upsert=True,
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
