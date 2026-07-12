"""CaminoBrain — pilot pulse brain (trend follower).

Doctrine: balanced personality (×1.00 confidence multiplier).
Runs both lanes but leans equity. Wraps `NeutralAdversarialBrain`
via the shared `NeutralAdversarialPulseBrain` base.

2026-07-12: refactored to inherit from `_pulse_base` when P2
migration added GTO / Barracuda / Hellcat as pulse brains. All 4
brains share identical orchestration; only the identity constants
+ personality multiplier differ. Camino's original per-file
implementation lives in `_pulse_base.NeutralAdversarialPulseBrain`
unchanged — the base was extracted from this file verbatim.

Audit checklist: `/app/memory/CAMINO_RUNNER_AUDIT.md`.
"""
from __future__ import annotations

from mc_brains._pulse_base import (
    NeutralAdversarialPulseBrain,
    PulseManifestHint,
)


# Backward-compat alias — some tests + the pulse loop import this
# name directly. Kept indefinitely to avoid a rename cascade;
# `PulseManifestHint` is the canonical name going forward.
CaminoManifestHint = PulseManifestHint


class CaminoBrain(NeutralAdversarialPulseBrain):
    """Trend-follower pilot brain."""

    PULSE_ID = "camino"
    CORE_BRAIN_ID = "alpha"
    DISPLAY_NAME = "Camino"
    RATIONALE_TAG = "trend"
