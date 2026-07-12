"""BarracudaBrain — opportunistic pulse brain.

Doctrine: **opportunistic**. Personality multiplier ×1.15 —
trips the ladder faster on a strong read than the balanced
brain. Runs both lanes.

Wraps the shared `NeutralAdversarialPulseBrain` base — identical
orchestration to Camino, differs ONLY in the personality
multiplier (via CORE_BRAIN_ID="camaro").

2026-07-12 (iter-28c, P2 step 2): migrated from `runner.py` to
this pulse-first path.
"""
from __future__ import annotations

from mc_brains._pulse_base import NeutralAdversarialPulseBrain


class BarracudaBrain(NeutralAdversarialPulseBrain):
    """Opportunistic brain — leans into strong reads."""

    PULSE_ID = "barracuda"
    CORE_BRAIN_ID = "camaro"
    DISPLAY_NAME = "Barracuda"
    RATIONALE_TAG = "opportunistic"
