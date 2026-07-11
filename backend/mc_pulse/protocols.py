"""Brain protocol — typing-only.

Every brain in `/app/backend/mc_brains/` implements this protocol.
The pulse loop only ever sees `Brain`; it doesn't know Camino from
Hellcat. That's the boundary.

Design freeze: `MC_PULSE.md` §1, §3, §9. MC standardizes inputs and
orchestration; brains own interpretation. Do NOT push interpretation
logic into this file — it's a shape, not a base class.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional, Protocol, runtime_checkable

from mc_pulse.snapshot import MarketSnapshot
from mc_arbiter.models import ModelOpinion


@runtime_checkable
class Brain(Protocol):
    """A brain is a Python object with an id, a set of lanes it
    trades, a cadence, a timeout, and TWO methods.

    Nothing else. No Mongo, no HTTP, no direct broker calls, no
    seat_key knowledge. Those are MC's responsibilities.
    """
    id: str
    lanes: frozenset[str]          # subset of {"equity", "crypto"}
    cadence_seconds: int           # this brain's preferred tick interval
    evaluation_timeout_seconds: float

    def should_evaluate(
        self,
        *,
        now: datetime,
        snapshot: MarketSnapshot,
    ) -> bool:
        """Called by MC every pulse. Returns True if the brain
        wants a shot at THIS snapshot. Lets faster and slower
        brains coexist on the same pulse cadence — MC pulses at
        the fastest useful interval, brains no-op the ticks that
        aren't theirs.
        """
        ...

    async def evaluate(
        self,
        snapshot: MarketSnapshot,
    ) -> Optional[ModelOpinion]:
        """Given an immutable snapshot, return an opinion or None.

        Returning None is a legitimate answer ("I looked and had
        nothing to say"). Different from returning `direction=FLAT`
        — FLAT is graded (the brain expressed a passive view);
        None means the brain declined to speak at all.
        """
        ...
