"""Research Layer schemas — frozen dataclasses, intentionally tiny.

These shapes are the *only* contract between the Strategy Lab and any
brain that wants to consume its output. Keep them additive: anything
the brain doesn't recognize should be ignorable, never crashing.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


Direction = Literal["BUY", "SELL", "HOLD"]


@dataclass(frozen=True)
class MarketFeatureFrame:
    """Snapshot of the most recent bar + derived indicators.

    Built once per (symbol, lane, timeframe) per evaluation pass. Fed
    into every strategy in `strategy_lab.STRATEGIES`. Floats are
    Python-native (no numpy) so JSON serialization is trivial and the
    evidence payload is portable to the brain's prompt context.
    """

    symbol: str
    lane: str
    close: float
    vwap: float | None = None
    rsi_14: float | None = None
    macd: float | None = None
    macd_signal: float | None = None
    macd_hist: float | None = None
    atr_14: float | None = None
    atr_14_pct: float | None = None
    volume: float | None = None
    rvol: float | None = None
    spread_bps: float | None = None
    bars_seen: int = 0


@dataclass(frozen=True)
class StrategySignal:
    """Output of a single strategy. Score in [0, 1], direction in
    {BUY, SELL, HOLD}. `reasons` is a tagging list — short tokens the
    UI can render as chips without further parsing.

    This is NOT a trade intent. It is *evidence* the brain weighs.
    """

    strategy_id: str
    symbol: str
    lane: str
    direction: Direction
    score: float
    confidence: float
    reasons: list[str] = field(default_factory=list)
