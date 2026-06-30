"""Brain personalities — deterministic strategy math, no LLM, no gates.

Each brain returns a Signal:
    { verdict: "BUY"|"SELL"|"HOLD", confidence: 0.0-1.0, reason: str }

Personality is BAKED IN — Camino is always trend, Barracuda always
mean-reversion, etc. Tunables (min_confidence, min_gap) live in the
brain_registry collection but defaults are encoded here so the
trader can run before the registry exists.

The math is deliberately simple. The original doctrine pin was that
brains are personalities, not magic — so we encode the personality
as a few-line rule rather than a multi-layer ML stack.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional


Verdict = Literal["BUY", "SELL", "HOLD"]


@dataclass(frozen=True)
class Signal:
    brain: str
    verdict: Verdict
    confidence: float
    reason: str


def _safe_div(n: float, d: float) -> float:
    if d in (0, 0.0):
        return 0.0
    return n / d


# ── Camino — trend continuation ──────────────────────────────────
def camino(data: dict) -> Signal:
    """Trend continuation. BUY when price is above SMA20 with RSI
    not yet overbought; SELL when below SMA20 with RSI not oversold.
    Confidence scales with the distance from the average."""
    price = data.get("last_price")
    sma = data.get("sma_20")
    rsi = data.get("rsi_14")
    if price is None or sma is None or rsi is None:
        return Signal("camino", "HOLD", 0.0, "missing_inputs")
    dist = _safe_div(price - sma, sma)
    if dist > 0.005 and rsi < 70:
        conf = min(0.95, 0.5 + min(0.45, dist * 8))
        return Signal("camino", "BUY", conf, f"trend_up dist={dist:.4f} rsi={rsi:.1f}")
    if dist < -0.005 and rsi > 30:
        conf = min(0.95, 0.5 + min(0.45, -dist * 8))
        return Signal("camino", "SELL", conf, f"trend_down dist={dist:.4f} rsi={rsi:.1f}")
    return Signal("camino", "HOLD", 0.3, f"flat dist={dist:.4f} rsi={rsi:.1f}")


# ── Barracuda — mean reversion ───────────────────────────────────
def barracuda(data: dict) -> Signal:
    """Mean reversion. BUY when RSI<30 (oversold). SELL when RSI>70.
    Confidence is how extreme the RSI is."""
    rsi = data.get("rsi_14")
    if rsi is None:
        return Signal("barracuda", "HOLD", 0.0, "missing_rsi")
    if rsi < 30:
        conf = min(0.95, 0.55 + (30 - rsi) * 0.02)
        return Signal("barracuda", "BUY", conf, f"oversold rsi={rsi:.1f}")
    if rsi > 70:
        conf = min(0.95, 0.55 + (rsi - 70) * 0.02)
        return Signal("barracuda", "SELL", conf, f"overbought rsi={rsi:.1f}")
    return Signal("barracuda", "HOLD", 0.3, f"neutral rsi={rsi:.1f}")


# ── Hellcat — breakout ───────────────────────────────────────────
def hellcat(data: dict) -> Signal:
    """Breakout. BUY when price closes above the 20-day high. SELL
    when price closes below the 20-day low. Falls back to BB-position
    if highs/lows aren't supplied."""
    price = data.get("last_price")
    h20 = data.get("high_20")
    l20 = data.get("low_20")
    bb_pos = data.get("bb_position")  # 0.0 (lower) to 1.0 (upper)
    if price is None:
        return Signal("hellcat", "HOLD", 0.0, "missing_price")
    if h20 and price > h20:
        return Signal("hellcat", "BUY", 0.75, f"breakout_high px={price} h20={h20}")
    if l20 and price < l20:
        return Signal("hellcat", "SELL", 0.75, f"breakout_low px={price} l20={l20}")
    if bb_pos is not None:
        if bb_pos > 0.95:
            return Signal("hellcat", "BUY", 0.65, f"bb_breakout_high pos={bb_pos:.2f}")
        if bb_pos < 0.05:
            return Signal("hellcat", "SELL", 0.65, f"bb_breakout_low pos={bb_pos:.2f}")
    return Signal("hellcat", "HOLD", 0.25, "no_breakout")


# ── GTO — momentum ───────────────────────────────────────────────
def gto(data: dict) -> Signal:
    """Momentum. BUY when MACD>signal AND RSI rising through 50;
    SELL when MACD<signal AND RSI falling through 50."""
    macd = data.get("macd")
    macd_signal = data.get("macd_signal")
    rsi = data.get("rsi_14")
    if macd is None or macd_signal is None or rsi is None:
        return Signal("gto", "HOLD", 0.0, "missing_inputs")
    gap = macd - macd_signal
    if gap > 0 and rsi > 50:
        conf = min(0.92, 0.5 + min(0.4, abs(gap) * 4))
        return Signal("gto", "BUY", conf, f"momentum_up macd_gap={gap:.4f} rsi={rsi:.1f}")
    if gap < 0 and rsi < 50:
        conf = min(0.92, 0.5 + min(0.4, abs(gap) * 4))
        return Signal("gto", "SELL", conf, f"momentum_down macd_gap={gap:.4f} rsi={rsi:.1f}")
    return Signal("gto", "HOLD", 0.3, f"mixed macd_gap={gap:.4f} rsi={rsi:.1f}")


BRAIN_FN = {
    "camino": camino,
    "barracuda": barracuda,
    "hellcat": hellcat,
    "gto": gto,
}


def run_brain(brain: str, data: dict) -> Optional[Signal]:
    fn = BRAIN_FN.get((brain or "").lower())
    if not fn:
        return None
    return fn(data)
