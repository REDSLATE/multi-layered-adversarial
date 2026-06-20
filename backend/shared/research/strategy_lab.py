"""Strategy Lab — scoring functions over a `MarketFeatureFrame`.

Each strategy is a pure function: feature frame in, `StrategySignal`
out. No state, no I/O, no broker — the Lab can only *score*. The
brains read these as evidence, weigh against their own doctrine, then
emit (or don't emit) an intent. Seat / RoadGuard remain the only
execution gates.

Doctrine reminders (encoded by the surface):
  * No strategy returns an order ID, broker name, or anything routable.
  * `direction == "HOLD"` is the default — strategies must affirm BUY
    or SELL with conviction, never default to a side.
  * `score` is bounded to [0, 1] and clipped here so a buggy rule
    can't poison the downstream evidence stream.
"""
from __future__ import annotations

from typing import Callable

from .schemas import MarketFeatureFrame, StrategySignal


# Confidence floor for any non-HOLD direction. Brains can still
# down-weight; this is just so a 0.0-score signal can't look credible.
_CONFIDENCE_FLOOR = 0.40
_BUY_THRESHOLD = 0.55
_SELL_THRESHOLD = 0.55


def _clip01(x: float) -> float:
    return max(0.0, min(1.0, x))


def large_cap_momentum(f: MarketFeatureFrame) -> StrategySignal:
    """Long-bias trend-following score for equities.

    Adds points for: price above VWAP, RSI in healthy upper-mid range,
    MACD line above signal line, above-average volume. Never returns
    SELL — this strategy only *affirms* longs; the absence of momentum
    means HOLD, not short.
    """
    score = 0.0
    reasons: list[str] = []

    if f.vwap is not None and f.close > f.vwap:
        score += 0.25
        reasons.append("above_vwap")

    if f.rsi_14 is not None and 50.0 <= f.rsi_14 <= 70.0:
        score += 0.20
        reasons.append("healthy_rsi")

    if (
        f.macd is not None
        and f.macd_signal is not None
        and f.macd > f.macd_signal
    ):
        score += 0.25
        reasons.append("macd_bullish")

    if f.rvol is not None and f.rvol >= 1.5:
        score += 0.20
        reasons.append("volume_confirmed")

    if f.bars_seen < 50:
        # Not enough history for SMA-50 / MACD warm-up to be reliable.
        reasons.append("warmup")

    direction = "BUY" if score >= _BUY_THRESHOLD and f.bars_seen >= 50 else "HOLD"
    return StrategySignal(
        strategy_id="large_cap_momentum_v1",
        symbol=f.symbol,
        lane=f.lane,
        direction=direction,
        score=_clip01(score),
        confidence=_clip01(_CONFIDENCE_FLOOR + score) if direction != "HOLD" else _clip01(score),
        reasons=reasons,
    )


def crypto_breakdown(f: MarketFeatureFrame) -> StrategySignal:
    """Short-bias breakdown score for crypto pairs.

    Adds points for: price below VWAP, weak RSI, MACD line below
    signal, above-average volume. Subtracts when the spread is wide
    (>200 bps) — Kraken Pro spreads blow up under stress and a tight
    short is worse than no short.
    """
    score = 0.0
    reasons: list[str] = []

    if f.vwap is not None and f.close < f.vwap:
        score += 0.25
        reasons.append("below_vwap")

    if f.rsi_14 is not None and f.rsi_14 < 45.0:
        score += 0.20
        reasons.append("weak_rsi")

    if (
        f.macd is not None
        and f.macd_signal is not None
        and f.macd < f.macd_signal
    ):
        score += 0.25
        reasons.append("macd_bearish")

    if f.rvol is not None and f.rvol >= 1.3:
        score += 0.20
        reasons.append("volume_confirmed")

    if f.spread_bps is not None and f.spread_bps > 200.0:
        score -= 0.10
        reasons.append("wide_spread_penalty")

    if f.bars_seen < 50:
        reasons.append("warmup")

    direction = (
        "SELL" if score >= _SELL_THRESHOLD and f.bars_seen >= 50 else "HOLD"
    )
    return StrategySignal(
        strategy_id="crypto_breakdown_v1",
        symbol=f.symbol,
        lane=f.lane,
        direction=direction,
        score=_clip01(score),
        confidence=_clip01(_CONFIDENCE_FLOOR + score) if direction != "HOLD" else _clip01(score),
        reasons=reasons,
    )


# Strategy registry — keyed by lane. `score_strategies` picks the
# right set based on the feature frame's lane so callers don't have
# to branch.
STRATEGIES: dict[str, list[Callable[[MarketFeatureFrame], StrategySignal]]] = {
    "equity": [large_cap_momentum],
    "crypto": [crypto_breakdown],
}


def score_strategies(f: MarketFeatureFrame) -> list[StrategySignal]:
    """Run every strategy registered for the frame's lane.

    Unknown lanes return [] — Strategy Lab silently abstains rather
    than fabricating a signal. The brain can then decide whether the
    absence of evidence matters for its own doctrine.
    """
    funcs = STRATEGIES.get(f.lane.lower(), [])
    return [fn(f) for fn in funcs]
