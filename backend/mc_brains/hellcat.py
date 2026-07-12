"""HellcatBrain — execution-safety pulse brain (P7a).

Doctrine (operator, 2026-07-12): "Hellcat should not simply be
another directional strategy. Its strongest identity is as the
brain that asks whether the setup is tradable under spread,
liquidity, volatility, and news conditions."

Historically Hellcat was the "aggressive" personality (×1.30
confidence multiplier). Post-P7a, its personality still amplifies
its raw output, but its RAW output now comes from
`ExecutionSafetyStrategy` — which produces high-conviction HOLDs
when the venue is hostile, and only weak directional reads when
execution is clean. This means Hellcat's ×1.30 multiplier now
amplifies EXECUTION CERTAINTY, not directional aggression, which
is the operator-intended cognitive role.

Wraps `PulseBrain` — identical orchestration
to Camino/GTO/Barracuda, differs only in `STRATEGY_CLS` +
`CORE_BRAIN_ID` (still `chevelle` for personality lookup).
"""
from __future__ import annotations

from mc_brains._pulse_base import PulseBrain
from mc_brains.strategies.execution_safety import ExecutionSafetyStrategy


class HellcatBrain(PulseBrain):
    """Execution-safety brain — vetoes trades under hostile venue conditions."""

    PULSE_ID = "hellcat"
    CORE_BRAIN_ID = "chevelle"
    DISPLAY_NAME = "Hellcat"
    RATIONALE_TAG = "execution"
    STRATEGY_CLS = ExecutionSafetyStrategy
