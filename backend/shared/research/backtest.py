"""Minimal vectorized backtest harness for Strategy Lab strategies.

Walks bar-by-bar over a window, regenerates a `MarketFeatureFrame` at
every step, scores the chosen strategy, and records a tiny outcome:
direction taken, forward return at the next bar, hit/miss tally. NOT a
production trading simulator — there is no slippage model, no
position sizing, no path-dependent state. Its job is to answer "did
this strategy historically point at the next bar's move?" so an
operator can spot a strategy that's pure noise BEFORE the brain
starts citing it.

Doctrine reminder: backtest never returns a trade or a P&L number the
broker could see. The outputs are evidence about the strategy, not
the symbol.
"""
from __future__ import annotations

from typing import Callable

from .features import build_features
from .schemas import StrategySignal


# Minimum window the snapshot needs for MACD-26 / SMA-50 to be warm.
_MIN_WARMUP_BARS = 50


def backtest_strategy(
    bars: list[dict],
    strategy: Callable[..., StrategySignal],
    symbol: str,
    lane: str,
    spread_bps: float | None = None,
) -> dict:
    """Run `strategy` across the bar window with a 1-bar forward look.

    Returns a summary dict suitable for logging or surfacing in the
    research panel:

        {
          "strategy_id": "...",
          "bars": N,
          "signals_total": int,    # non-HOLD steps
          "buys": int, "sells": int,
          "wins": int, "losses": int,
          "win_rate": float | None,
          "avg_forward_return_bps": float | None,
          "samples": [
            {"i": idx, "direction": "BUY", "score": 0.x,
             "forward_bps": 12.4, "win": True}, ...
          ],  # up to 10 most recent
        }
    """
    n = len(bars)
    if n < _MIN_WARMUP_BARS + 2:
        return {
            "strategy_id": strategy.__name__,
            "bars": n,
            "signals_total": 0,
            "buys": 0,
            "sells": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": None,
            "avg_forward_return_bps": None,
            "samples": [],
            "warmup_required": _MIN_WARMUP_BARS,
        }

    buys = sells = wins = losses = 0
    forward_bps_sum = 0.0
    samples: list[dict] = []

    for i in range(_MIN_WARMUP_BARS, n - 1):
        window = bars[: i + 1]
        f = build_features(symbol, lane, window, spread_bps=spread_bps)
        sig = strategy(f)
        if sig.direction == "HOLD":
            continue

        next_close = float(bars[i + 1]["c"])
        this_close = float(bars[i]["c"])
        if this_close <= 0:
            continue
        # Forward return in basis points; sign flipped for SELL so a
        # winning short shows up positive in the aggregate.
        raw_bps = (next_close - this_close) / this_close * 10_000
        forward_bps = raw_bps if sig.direction == "BUY" else -raw_bps
        win = forward_bps > 0

        if sig.direction == "BUY":
            buys += 1
        else:
            sells += 1
        if win:
            wins += 1
        else:
            losses += 1
        forward_bps_sum += forward_bps

        if len(samples) >= 10:
            samples.pop(0)
        samples.append({
            "i": i,
            "direction": sig.direction,
            "score": round(sig.score, 4),
            "forward_bps": round(forward_bps, 2),
            "win": win,
        })

    signals_total = buys + sells
    return {
        "strategy_id": strategy.__name__,
        "bars": n,
        "signals_total": signals_total,
        "buys": buys,
        "sells": sells,
        "wins": wins,
        "losses": losses,
        "win_rate": (wins / signals_total) if signals_total else None,
        "avg_forward_return_bps": (
            forward_bps_sum / signals_total if signals_total else None
        ),
        "samples": samples,
    }
