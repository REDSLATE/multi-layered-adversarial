"""GtoBrain — disciplined pulse brain.

Doctrine: **disciplined**. Personality multiplier ×0.85 —
requires more evidence than the balanced/opportunistic brains
before its confidence trips the learning ladder's promotion
threshold. Runs both lanes; no lane-lean.

Wraps the shared `PulseBrain` base — identical
orchestration to Camino, differs ONLY in the personality
multiplier (via CORE_BRAIN_ID="redeye").

2026-07-12 (iter-28c, P2 step 2): migrated from `runner.py` to
this pulse-first path. The runner still writes shared_intents in
comparison mode; the pulse writes envelopes to
`mc_opinions_compare`. Parity trend observed via
`/api/mc/parity/gto/history`.
"""
from __future__ import annotations

from mc_brains._pulse_base import PulseBrain
from mc_brains.strategies.momentum_confirmation import (
    MomentumConfirmationStrategy,
)


class GtoBrain(PulseBrain):
    """Disciplined brain — requires more evidence than balanced."""

    PULSE_ID = "gto"
    CORE_BRAIN_ID = "redeye"
    DISPLAY_NAME = "GTO"
    RATIONALE_TAG = "momentum"
    STRATEGY_CLS = MomentumConfirmationStrategy
