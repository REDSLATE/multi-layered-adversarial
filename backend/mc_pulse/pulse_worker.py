"""Pulse background worker — the 15s cadence loop.

Doctrine (design freeze §4): the pulse worker owns the critical
path (snapshot → evaluate → persist). Non-critical maintenance
(grader, rollups) runs on separate workers with separate failure
envelopes. This worker must not block on Atlas or the grader.

Runtime toggles (env):
    MC_PULSE_CADENCE_S       (default 15)    — tick interval
    MC_PULSE_COMPARE_ONLY    (default false) — write envelopes to
        `mc_opinions_compare` for parity work. When false (default),
        envelopes go to `mc_seats` where the arbiter can read them.
        Set true only to restore the pre-2026-07-13 parity mode.
    MC_PULSE_AUTO_ARBITRATE  (default true)  — after every tick,
        arbitrate every seat_key touched. Only meaningful when
        `MC_PULSE_COMPARE_ONLY=false`. In LIVE runtime mode this
        closes the pulse→arbiter→trader loop. Whether an intent
        actually reaches the broker is still gated by:
          1. arbiter runtime_mode (DISARMED → decision only, no intent)
          2. trading_controls.enabled (master switch preflight)
          3. seat/risk/broker chain in auto_router

The arbiter's actual runtime mode (DISARMED/LIVE) is read from the
persisted state each tick — a single cheap doc read — so an
operator flip via `POST /api/mc/arbiter/runtime-mode` takes effect
on the next pulse without a restart.
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


def _env_bool(key: str, default: bool = False) -> bool:
    return os.environ.get(key, str(default)).strip().lower() in {
        "true", "1", "yes", "on",
    }


async def _read_runtime_mode_safe() -> str:
    """Cheap single-doc read of the persisted arbiter runtime mode.
    Falls back to `DISARMED` on any error — that's the safe default
    (arbitration still runs, no intent is emitted)."""
    try:
        from mc_arbiter.arbiter import get_runtime_mode  # noqa: WPS433
        mode = await get_runtime_mode()
        return mode.value
    except Exception:  # noqa: BLE001
        return "DISARMED"


async def _pulse_loop() -> None:
    """The critical-path loop. Runs one pulse_tick per cadence
    interval. NEVER holds a lock across ticks — a slow pulse
    that overruns the cadence is flagged on the receipt
    (`overrun=True`), but the next pulse still fires as scheduled.
    """
    compare_only = _env_bool("MC_PULSE_COMPARE_ONLY", False)
    auto_arbitrate = _env_bool("MC_PULSE_AUTO_ARBITRATE", True)
    logger.info(
        "mc_pulse loop starting cadence=%ss compare_only=%s auto_arbitrate=%s",
        PULSE_CADENCE_SECONDS, compare_only, auto_arbitrate,
    )
    while True:
        loop_started = asyncio.get_event_loop().time()
        try:
            # 2026-07-15 iter-30 P4c: outer bound on the whole
            # tick so a slow-Atlas moment can't stall the pulse
            # loop indefinitely. Cap = 3× cadence — long enough
            # for legitimate slow-tape ticks (crypto refresh does
            # a batch of Kraken calls), short enough that a
            # genuine hang gets caught before the next tick
            # would fire. On TimeoutError the pulse skips this
            # cycle and the next one starts on schedule; we
            # write NOTHING because a half-written tick would
            # corrupt seat state.
            pulse_deadline = max(30.0, PULSE_CADENCE_SECONDS * 3.0)
            snapshots = await asyncio.wait_for(
                build_all(), timeout=pulse_deadline,
            )
            # Read arbiter runtime mode fresh each tick so an
            # operator flip takes effect on the next pulse.
            runtime_mode = await _read_runtime_mode_safe()
            receipt = await asyncio.wait_for(
                pulse_tick(
                    snapshots,
                    cadence_seconds=PULSE_CADENCE_SECONDS,
                    runtime_mode=runtime_mode,
                    compare_only=compare_only,
                    auto_arbitrate=auto_arbitrate,
                ),
                timeout=pulse_deadline,
            )
            logger.info(
                "pulse tick pulse_id=%s snapshots=%d "
                "brains_completed=%d brains_failed=%d "
                "arbitrations=%d intents=%d "
                "runtime_mode=%s orchestration_ok=%s overrun=%s "
                "orchestration_error=%s",
                receipt.pulse_id,
                receipt.snapshot_count,
                len(receipt.brains_completed),
                len(receipt.brains_failed),
                receipt.arbitrations_completed,
                receipt.intents_emitted,
                runtime_mode,
                receipt.orchestration_ok,
                receipt.overrun,
                receipt.orchestration_error or "-",
            )
        except asyncio.CancelledError:
            logger.info("mc_pulse loop cancelled")
            raise
        except asyncio.TimeoutError:
            # 2026-07-15 iter-30 P4c: pulse hit the outer deadline
            # (3× cadence). This is exactly the 9-min-hang scenario
            # we saw on production 2026-07-15 12:24 when Atlas was
            # degraded. Skip this tick, LOG loudly (the operator
            # needs to see this signal, unlike the silent hang),
            # and let the next tick fire fresh on cadence.
            logger.warning(
                "mc_pulse tick deadline exceeded (%.1fs cap) — "
                "skipping this tick, next one fires on schedule. "
                "This usually means Atlas is slow; check "
                "`degraded=true` in the intent-clearance-funnel "
                "endpoint.",
                pulse_deadline,
            )
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
