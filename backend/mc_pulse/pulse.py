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
from mc_pulse.containment import evaluate_brain
from mc_pulse.envelope import OpinionEnvelope
from mc_pulse.protocols import Brain
from mc_pulse.receipt import BrainFailure, PulseReceipt, persist_receipt
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
    tasks = []
    task_meta = []  # parallel array of (brain_id, symbol, seat_key)
    for snap in snapshots:
        for brain in registry.for_lane(snap.lane):
            if not brain.should_evaluate(now=now, snapshot=snap):
                continue
            seat_key = build_seat_key(snap.lane, snap.symbol, now)
            tasks.append(evaluate_brain(brain, snap, receipt.pulse_id, seat_key=seat_key))
            task_meta.append((brain.id, snap.symbol, seat_key))

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
    for (brain_id, _sym, _seat), (env, fail) in zip(task_meta, results):
        if env is not None:
            envelopes.append(env)
            brains_completed.add(brain_id)
        elif fail is not None:
            receipt.brains_failed.append(fail)
        else:
            # Brain returned None — completed cleanly, just had
            # nothing to say. Counts as completed.
            brains_completed.add(brain_id)
    receipt.brains_completed = sorted(brains_completed)

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


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
