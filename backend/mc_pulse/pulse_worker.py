"""Pulse background worker — the 15s cadence loop.

Doctrine (design freeze §4): the pulse worker owns the critical
path (snapshot → evaluate → persist). Non-critical maintenance
(grader, rollups) runs on separate workers with separate failure
envelopes. This worker must not block on Atlas or the grader.

`compare_only=True` at v0.1 — envelopes go to `mc_opinions_compare`,
NOT `mc_seats`. Nothing reaches the arbiter or the trader from
this path during migration steps 2–5.
"""
from __future__ import annotations

import asyncio
import logging
import os

from fastapi import FastAPI

from mc_pulse.pulse import pulse_tick
from mc_pulse.snapshot_service import build_all

logger = logging.getLogger("mc_pulse.worker")

# Cadence: 15s at v0.1. Design freeze §3: MC pulses at the
# fastest useful interval; brains decide via `should_evaluate`
# when they actually speak. Camino sets cadence_seconds=30 so it
# no-ops every other pulse — parity with its old runner cadence.
PULSE_CADENCE_SECONDS = int(os.environ.get("MC_PULSE_CADENCE_S", "15"))


async def _pulse_loop() -> None:
    """The critical-path loop. Runs one pulse_tick per cadence
    interval. NEVER holds a lock across ticks — a slow pulse
    that overruns the cadence is flagged on the receipt
    (`overrun=True`), but the next pulse still fires as scheduled.
    """
    logger.info(
        "mc_pulse loop starting cadence=%ss compare_only=True",
        PULSE_CADENCE_SECONDS,
    )
    while True:
        loop_started = asyncio.get_event_loop().time()
        try:
            snapshots = await build_all()
            receipt = await pulse_tick(
                snapshots,
                cadence_seconds=PULSE_CADENCE_SECONDS,
                runtime_mode="DISARMED",     # arbiter still owns real routing
                compare_only=True,           # writes to mc_opinions_compare
            )
            logger.info(
                "pulse tick pulse_id=%s snapshots=%d "
                "brains_completed=%d brains_failed=%d "
                "orchestration_ok=%s overrun=%s",
                receipt.pulse_id,
                receipt.snapshot_count,
                len(receipt.brains_completed),
                len(receipt.brains_failed),
                receipt.orchestration_ok,
                receipt.overrun,
            )
        except asyncio.CancelledError:
            logger.info("mc_pulse loop cancelled")
            raise
        except Exception:  # noqa: BLE001
            # A raise here is orchestration-level — brain-level
            # failures were already caught inside pulse_tick via
            # containment. Log the trace and keep looping; the
            # next pulse gets a fresh chance.
            logger.exception("mc_pulse loop error (continuing)")

        # Sleep for the balance of the cadence interval.
        elapsed = asyncio.get_event_loop().time() - loop_started
        sleep_for = max(0.0, PULSE_CADENCE_SECONDS - elapsed)
        await asyncio.sleep(sleep_for)


def start_pulse_worker(app: FastAPI) -> None:
    """Attach the pulse loop to the FastAPI app's state so
    lifespan shutdown can cancel it cleanly."""
    if getattr(app.state, "mc_pulse_task", None):
        logger.warning("mc_pulse worker already running")
        return
    task = asyncio.create_task(_pulse_loop(), name="mc_pulse_loop")
    app.state.mc_pulse_task = task


async def stop_pulse_worker(app: FastAPI) -> None:
    task = getattr(app.state, "mc_pulse_task", None)
    if not task or task.done():
        return
    task.cancel()
    try:
        await asyncio.wait_for(task, timeout=10)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        pass
    logger.info("mc_pulse worker stopped")
    app.state.mc_pulse_task = None
