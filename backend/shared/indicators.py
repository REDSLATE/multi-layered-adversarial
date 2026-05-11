"""Pure-Python technical indicators. No pandas/numpy dependency.

Each function takes a list of OHLCV bar dicts (oldest → newest, each with
keys o,h,l,c,v as floats) and returns either a list of values aligned to
input length (None for warm-up periods) or a single latest value as a
plain float.

Doctrine: these are shared evidence builders — same math for every brain.
Interpretation belongs to the brain, not to this module.
"""
from __future__ import annotations

from typing import Iterable, Optional


# ──────────────────────── moving averages ────────────────────────

def sma(values: list[float], period: int) -> list[Optional[float]]:
    """Simple moving average. Returns None for the first `period-1` slots."""
    out: list[Optional[float]] = []
    s = 0.0
    for i, v in enumerate(values):
        s += v
        if i >= period:
            s -= values[i - period]
        out.append(s / period if i >= period - 1 else None)
    return out


def ema(values: list[float], period: int) -> list[Optional[float]]:
    """Exponential moving average using the classic 2/(N+1) smoothing.
    Seeded by SMA(period) on the first warm-up window."""
    out: list[Optional[float]] = [None] * len(values)
    if len(values) < period:
        return out
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    k = 2.0 / (period + 1)
    prev = seed
    for i in range(period, len(values)):
        prev = (values[i] - prev) * k + prev
        out[i] = prev
    return out


# ──────────────────────── RSI ────────────────────────

def rsi(values: list[float], period: int = 14) -> list[Optional[float]]:
    """Wilder's RSI. None until `period` deltas are available."""
    out: list[Optional[float]] = [None] * len(values)
    if len(values) <= period:
        return out
    gains: list[float] = [0.0]
    losses: list[float] = [0.0]
    for i in range(1, len(values)):
        diff = values[i] - values[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))
    # Seed with simple average of first `period` deltas (indexes 1..period).
    avg_gain = sum(gains[1:period + 1]) / period
    avg_loss = sum(losses[1:period + 1]) / period
    out[period] = _rsi_from(avg_gain, avg_loss)
    for i in range(period + 1, len(values)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        out[i] = _rsi_from(avg_gain, avg_loss)
    return out


def _rsi_from(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


# ──────────────────────── MACD ────────────────────────

def macd(values: list[float], fast: int = 12, slow: int = 26, signal: int = 9) -> dict:
    """Classic MACD. Returns dict of three aligned series: macd, signal, hist."""
    fast_ema = ema(values, fast)
    slow_ema = ema(values, slow)
    macd_line: list[Optional[float]] = [
        (a - b) if (a is not None and b is not None) else None
        for a, b in zip(fast_ema, slow_ema)
    ]
    # Signal EMA needs `signal` consecutive non-None MACD values to seed.
    macd_valid = [v for v in macd_line if v is not None]
    if len(macd_valid) < signal:
        signal_line: list[Optional[float]] = [None] * len(values)
    else:
        start = next(i for i, v in enumerate(macd_line) if v is not None)
        # Compute EMA on the contiguous valid slice, then re-align.
        valid_signal = ema(macd_valid, signal)
        signal_line = [None] * len(values)
        for i, v in enumerate(valid_signal):
            signal_line[start + i] = v
    hist = [
        (m - s) if (m is not None and s is not None) else None
        for m, s in zip(macd_line, signal_line)
    ]
    return {"macd": macd_line, "signal": signal_line, "hist": hist}


# ──────────────────────── Bollinger Bands ────────────────────────

def bollinger(values: list[float], period: int = 20, k: float = 2.0) -> dict:
    """Bollinger Bands. Returns dict with mid/upper/lower aligned series.

    Width is reported as percentage of the mid-band so brains can spot
    a "squeeze" without re-doing math.
    """
    mid = sma(values, period)
    upper: list[Optional[float]] = [None] * len(values)
    lower: list[Optional[float]] = [None] * len(values)
    width_pct: list[Optional[float]] = [None] * len(values)
    for i in range(period - 1, len(values)):
        window = values[i - period + 1:i + 1]
        m = mid[i]
        if m is None:
            continue
        var = sum((x - m) ** 2 for x in window) / period
        sd = var ** 0.5
        upper[i] = m + k * sd
        lower[i] = m - k * sd
        width_pct[i] = ((upper[i] - lower[i]) / m * 100.0) if m else None
    return {"mid": mid, "upper": upper, "lower": lower, "width_pct": width_pct}


# ──────────────────────── ATR ────────────────────────

def atr(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> list[Optional[float]]:
    """Average True Range, Wilder smoothing."""
    n = len(closes)
    if n < period + 1 or len(highs) != n or len(lows) != n:
        return [None] * n
    trs: list[float] = [0.0]
    for i in range(1, n):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)
    out: list[Optional[float]] = [None] * n
    seed = sum(trs[1:period + 1]) / period
    out[period] = seed
    prev = seed
    for i in range(period + 1, n):
        prev = (prev * (period - 1) + trs[i]) / period
        out[i] = prev
    return out


# ──────────────────────── snapshot builder ────────────────────────

def build_snapshot(bars: list[dict]) -> dict:
    """Compute a complete indicator snapshot from a window of bars.

    Caller guarantees `bars` is sorted ascending by timestamp. Returns
    only the last (most recent) value for each series + a tiny tail so
    brains can see direction without paging the whole history.
    """
    if not bars:
        return {"ready": False, "bars_seen": 0}

    closes = [float(b["c"]) for b in bars]
    highs = [float(b["h"]) for b in bars]
    lows = [float(b["l"]) for b in bars]
    last_idx = len(bars) - 1

    sma20 = sma(closes, 20)
    sma50 = sma(closes, 50)
    sma200 = sma(closes, 200)
    ema12 = ema(closes, 12)
    ema26 = ema(closes, 26)
    rsi14 = rsi(closes, 14)
    macd_out = macd(closes, 12, 26, 9)
    bb = bollinger(closes, 20, 2.0)
    atr14 = atr(highs, lows, closes, 14)

    def _last(series: list) -> Optional[float]:
        return series[last_idx] if series and series[last_idx] is not None else None

    def _tail(series: list, n: int = 4) -> list:
        return [None if v is None else round(float(v), 6) for v in series[-n:]]

    last_close = closes[last_idx]
    bb_mid = _last(bb["mid"])
    bb_upper = _last(bb["upper"])
    bb_lower = _last(bb["lower"])
    bb_pos = None
    if bb_mid is not None and bb_upper is not None and bb_lower is not None and bb_upper != bb_lower:
        # Position of close within the band: 0.0 lower band, 1.0 upper band.
        bb_pos = (last_close - bb_lower) / (bb_upper - bb_lower)

    return {
        "ready": True,
        "bars_seen": len(bars),
        "last_close": last_close,
        "sma": {
            "20": _last(sma20), "50": _last(sma50), "200": _last(sma200),
        },
        "ema": {
            "12": _last(ema12), "26": _last(ema26),
        },
        "rsi14": _last(rsi14),
        "rsi14_tail": _tail(rsi14),
        "macd": {
            "macd": _last(macd_out["macd"]),
            "signal": _last(macd_out["signal"]),
            "hist": _last(macd_out["hist"]),
            "hist_tail": _tail(macd_out["hist"]),
        },
        "bbands": {
            "mid": bb_mid, "upper": bb_upper, "lower": bb_lower,
            "width_pct": _last(bb["width_pct"]),
            "position": bb_pos,
        },
        "atr14": _last(atr14),
        "atr14_pct": (
            (_last(atr14) / last_close * 100.0)
            if _last(atr14) is not None and last_close else None
        ),
    }
