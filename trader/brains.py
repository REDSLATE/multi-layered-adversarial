"""Brain personalities — deterministic strategy math, no LLM, no gates.

Each brain returns a Signal:
    { verdict: "BUY"|"SELL"|"HOLD", confidence: 0.0-1.0, reason: str }

Personality is BAKED IN — Camino is always trend, Barracuda always
mean-reversion, etc. Operator directive (2026-06-30): be more eager.
The previous textbook-strict rules (RSI < 30, 20-day breakouts, etc.)
left every receipt at HOLD because real markets rarely hit those
edges. Loosened thresholds — still principled, but engage earlier
in the move so weak directional setups still produce confidence
above the 0.55 fire threshold.

Each brain's STRUCTURE is unchanged (Camino still measures trend,
Barracuda still measures mean reversion). The CONSTANTS are looser.
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
    """Trend continuation. Loosened 2026-06-30:
        BUY when price is *anywhere* above SMA20 with RSI in [45, 75]
        SELL when price is below SMA20 with RSI in [25, 55]
    Confidence scales with distance and RSI alignment. A small move
    above SMA in a rising RSI environment is a real trend signal,
    not noise."""
    price = data.get("last_price")
    sma = data.get("sma_20")
    rsi = data.get("rsi_14")
    if price is None or sma is None or rsi is None:
        return Signal("camino", "HOLD", 0.0, "missing_inputs")
    dist = _safe_div(price - sma, sma)
    # BUY: any positive distance + RSI room to grow
    if dist > 0.0005 and 45 <= rsi <= 75:
        # Confidence: 0.55 base when conditions met, scale up with
        # distance (capped at 0.95).
        conf = 0.55 + min(0.40, dist * 30)
        return Signal("camino", "BUY", round(conf, 3),
                      f"trend_up dist={dist:+.4f} rsi={rsi:.1f}")
    # SELL: any negative distance + RSI room to fall
    if dist < -0.0005 and 25 <= rsi <= 55:
        conf = 0.55 + min(0.40, -dist * 30)
        return Signal("camino", "SELL", round(conf, 3),
                      f"trend_down dist={dist:+.4f} rsi={rsi:.1f}")
    return Signal("camino", "HOLD", 0.30,
                  f"flat dist={dist:+.4f} rsi={rsi:.1f}")


# ── Barracuda — mean reversion ───────────────────────────────────
def barracuda(data: dict) -> Signal:
    """Mean reversion. Loosened 2026-06-30:
        BUY when RSI < 45  (was: < 30 oversold-only)
        SELL when RSI > 55 (was: > 70 overbought-only)
    Confidence scales with how far from the 50 midpoint.
    """
    rsi = data.get("rsi_14")
    if rsi is None:
        return Signal("barracuda", "HOLD", 0.0, "missing_rsi")
    if rsi < 45:
        # Confidence 0.55 at RSI 45, scaling up as RSI drops.
        conf = 0.55 + min(0.40, (45 - rsi) * 0.018)
        return Signal("barracuda", "BUY", round(conf, 3),
                      f"below_mid rsi={rsi:.1f}")
    if rsi > 55:
        conf = 0.55 + min(0.40, (rsi - 55) * 0.018)
        return Signal("barracuda", "SELL", round(conf, 3),
                      f"above_mid rsi={rsi:.1f}")
    return Signal("barracuda", "HOLD", 0.30, f"midpoint rsi={rsi:.1f}")


# ── Hellcat — breakout ───────────────────────────────────────────
def hellcat(data: dict) -> Signal:
    """Breakout. Loosened 2026-06-30:
    Primary: price within 1% of the 20-day high (BUY) or low (SELL).
    Fallback: BB-position > 0.65 (BUY) or < 0.35 (SELL).
    Pre-breakout positioning is when the alpha lives — waiting for
    the actual high to print is too late for $10 size."""
    price = data.get("last_price")
    h20 = data.get("high_20")
    l20 = data.get("low_20")
    bb_pos = data.get("bb_position")
    if price is None:
        return Signal("hellcat", "HOLD", 0.0, "missing_price")
    # Pre-breakout proximity — distance to 20d high/low as % of range.
    if h20 and l20 and h20 > l20:
        range_pct = (h20 - l20) / l20
        dist_high = (h20 - price) / h20  # 0 means at high, positive means below
        dist_low = (price - l20) / l20
        if dist_high <= 0.01 and range_pct > 0.01:
            # Within 1% of 20-day high — pre-breakout. Strong setup.
            conf = 0.65 + min(0.30, (0.01 - dist_high) * 30)
            return Signal("hellcat", "BUY", round(conf, 3),
                          f"near_high px={price:.4f} h20={h20:.4f}")
        if dist_low <= 0.01 and range_pct > 0.01:
            conf = 0.65 + min(0.30, (0.01 - dist_low) * 30)
            return Signal("hellcat", "SELL", round(conf, 3),
                          f"near_low px={price:.4f} l20={l20:.4f}")
    # Bollinger fallback when high/low unknown OR mid-range.
    if bb_pos is not None:
        if bb_pos > 0.65:
            conf = 0.55 + min(0.40, (bb_pos - 0.65) * 1.2)
            return Signal("hellcat", "BUY", round(conf, 3),
                          f"bb_upper pos={bb_pos:.2f}")
        if bb_pos < 0.35:
            conf = 0.55 + min(0.40, (0.35 - bb_pos) * 1.2)
            return Signal("hellcat", "SELL", round(conf, 3),
                          f"bb_lower pos={bb_pos:.2f}")
    return Signal("hellcat", "HOLD", 0.30, "mid_range")


# ── GTO — momentum ───────────────────────────────────────────────
def gto(data: dict) -> Signal:
    """Momentum. Loosened 2026-06-30:
    MACD-signal cross is the trigger; RSI is a confirmation gate
    but not a hard filter. BUY when MACD > signal (regardless of
    RSI, with lower conf if RSI < 50). SELL when MACD < signal.
    Catches early momentum shifts instead of waiting for RSI
    to cross 50."""
    macd = data.get("macd")
    macd_signal = data.get("macd_signal")
    rsi = data.get("rsi_14")
    if macd is None or macd_signal is None:
        return Signal("gto", "HOLD", 0.0, "missing_macd")
    gap = macd - macd_signal
    if gap > 0:
        # BUY — momentum is positive. RSI > 50 adds confidence.
        rsi_bonus = 0.10 if (rsi is not None and rsi > 50) else 0.0
        conf = 0.55 + rsi_bonus + min(0.30, abs(gap) * 6)
        return Signal("gto", "BUY", round(conf, 3),
                      f"macd_up gap={gap:+.4f} rsi={(rsi or 0):.1f}")
    if gap < 0:
        rsi_bonus = 0.10 if (rsi is not None and rsi < 50) else 0.0
        conf = 0.55 + rsi_bonus + min(0.30, abs(gap) * 6)
        return Signal("gto", "SELL", round(conf, 3),
                      f"macd_down gap={gap:+.4f} rsi={(rsi or 0):.1f}")
    return Signal("gto", "HOLD", 0.30, "macd_cross")


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
