"""Per-brain containment: timeout + exception isolation.

Design freeze: `MC_PULSE.md` §5. One broken brain never silences
the others. `asyncio.gather(return_exceptions=False)` at the
pulse level then only raises on ORCHESTRATION failure, never on
brain failure — brain failures land here, get recorded, and
return `None`.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from mc_pulse.envelope import OpinionEnvelope
from mc_pulse.protocols import Brain
from mc_pulse.snapshot import MarketSnapshot
from mc_pulse.receipt import BrainFailure

logger = logging.getLogger("mc_pulse.containment")


async def evaluate_brain(
    brain: Brain,
    snapshot: MarketSnapshot,
    pulse_id: str,
    *,
    seat_key: str,
) -> tuple[Optional[OpinionEnvelope], Optional[BrainFailure]]:
    """Run one brain against one snapshot with hard containment.

    Returns `(envelope, None)` on success, `(None, failure)` on
    timeout or exception. NEVER raises — the caller
    (`pulse.pulse_tick`) uses `asyncio.gather` with
    `return_exceptions=False` and expects clean returns from
    every containment call.

    Doctrine: a broken Hellcat evaluation MUST NOT silence
    Camino, Barracuda, and GTO.
    """
    now_iso = _now_iso()
    try:
        opinion = await asyncio.wait_for(
            brain.evaluate(snapshot),
            timeout=brain.evaluation_timeout_seconds,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "brain evaluation timed out brain=%s symbol=%s snapshot=%s",
            brain.id, snapshot.symbol, snapshot.snapshot_id,
        )
        return None, BrainFailure(
            brain_id=brain.id,
            reason="evaluation_timeout",
            symbol=snapshot.symbol,
            lane=snapshot.lane,
            at=now_iso,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "brain evaluation raised brain=%s symbol=%s snapshot=%s",
            brain.id, snapshot.symbol, snapshot.snapshot_id,
        )
        return None, BrainFailure(
            brain_id=brain.id,
            reason="evaluation_error",
            exc_type=type(exc).__name__,
            symbol=snapshot.symbol,
            lane=snapshot.lane,
            at=now_iso,
        )

    if opinion is None:
        # Legitimate "I looked and had nothing to say." Not a
        # failure — just no envelope for this (brain, snapshot).
        return None, None

    envelope = OpinionEnvelope(
        pulse_id=pulse_id,
        brain_id=brain.id,
        seat_key=seat_key,
        snapshot_id=snapshot.snapshot_id,
        opinion=opinion,
        evaluated_at=datetime.now(timezone.utc),
    )
    return envelope, None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
