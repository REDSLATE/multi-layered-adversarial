"""HellcatBrain — aggressive pulse brain.

Doctrine: **aggressive**. Personality multiplier ×1.30 — trips
the ladder fastest of the 4 brains. This is a raw-conviction
brain; MC's exposure caps + kill switch are the guardrails, not
brain-side gates.

Wraps the shared `NeutralAdversarialPulseBrain` base — identical
orchestration to Camino, differs ONLY in the personality
multiplier (via CORE_BRAIN_ID="chevelle").

2026-07-12 (iter-28c, P2 step 2): migrated from `runner.py` to
this pulse-first path.
"""
from __future__ import annotations

from mc_brains._pulse_base import NeutralAdversarialPulseBrain


class HellcatBrain(NeutralAdversarialPulseBrain):
    """Aggressive brain — highest personality multiplier."""

    PULSE_ID = "hellcat"
    CORE_BRAIN_ID = "chevelle"
    DISPLAY_NAME = "Hellcat"
    RATIONALE_TAG = "aggressive"
