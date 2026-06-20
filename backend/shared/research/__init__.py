"""Research Layer — Strategy Lab.

Read-only analytical layer that produces *evidence* for the brains.
NEVER calls the broker. NEVER bypasses Seat / RoadGuard. The doctrine
is enforced by the surface alone: this package exports no submit
helper, only `build_features`, `score_strategies`, and
`attach_research_to_intent`.

Core rule (2026-02-20 operator directive, verbatim):
    Strategy Lab can score.
    Brains can opine.
    Seats can execute.
    RoadGuard can stop.
"""
from .schemas import Direction, MarketFeatureFrame, StrategySignal  # noqa: F401
from .features import build_features  # noqa: F401
from .strategy_lab import (  # noqa: F401
    STRATEGIES,
    crypto_breakdown,
    large_cap_momentum,
    score_strategies,
)
from .paradox_bridge import attach_research_to_intent  # noqa: F401
