"""Research Layer indicator helpers.

Pure-Python — deliberately NO pandas/numpy dependency to stay in step
with `shared.indicators` (the codebase's existing indicator module).
Heavy lifting (RSI / MACD / ATR / EMA) reuses `shared.indicators`;
this file only adds the two helpers the existing module lacks: VWAP
and rolling relative-volume (rvol).
"""
from __future__ import annotations

from typing import Optional


def vwap(bars: list[dict]) -> Optional[float]:
    """Cumulative VWAP over the supplied bar window.

    Args:
        bars: oldest → newest OHLCV bars (each with keys h, l, c, v).

    Returns:
        Latest VWAP value, or None if the window has zero cumulative
        volume (rare — happens on illiquid symbols or before the first
        traded bar). VWAP is *cumulative within the window* — callers
        that want intraday VWAP should pass only that day's bars.
    """
    if not bars:
        return None
    num = 0.0
    den = 0.0
    for b in bars:
        try:
            h = float(b["h"])
            l = float(b["l"])  # noqa: E741 — bar dict key, not a 1
            c = float(b["c"])
            v = float(b["v"])
        except (KeyError, TypeError, ValueError):
            continue
        typical = (h + l + c) / 3.0
        num += typical * v
        den += v
    if den <= 0:
        return None
    return num / den


def rvol(bars: list[dict], window: int = 20) -> Optional[float]:
    """Relative volume = last bar's volume / avg volume over `window`.

    Returns None when fewer than `window` bars are available or the
    average is zero. >1.0 means above-average volume on the latest bar.
    """
    if len(bars) < window:
        return None
    try:
        recent = [float(b["v"]) for b in bars[-window:]]
        last = float(bars[-1]["v"])
    except (KeyError, TypeError, ValueError):
        return None
    avg = sum(recent) / window
    if avg <= 0:
        return None
    return last / avg
