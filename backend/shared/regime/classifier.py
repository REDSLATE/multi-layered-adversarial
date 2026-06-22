"""Deterministic regime classifier (port of operator's
`shared_regime_classifier.py`, 2026-02-21).

Stateless, no ML, no Mongo, no network. Input: a `MarketSnapshot`
with the technical indicators the operator's classifier expects.
Output: a `RegimeResult` with primary + secondary regimes,
confidence, and an explainable reasons list.

Faithful port — verbatim thresholds and decision tree. The only
delta from the operator's artifact: package path lives at
`shared/regime/classifier.py` so the rest of the stack imports
`from shared.regime.classifier import classify_regime, Regime,
MarketSnapshot`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List


class Regime(str, Enum):
    LOW_VOL = "low_vol"
    NORMAL = "normal"
    HIGH_VOL = "high_vol"
    TREND_UP = "trend_up"
    TREND_DOWN = "trend_down"
    CHOP = "chop"
    SQUEEZE = "squeeze"
    BREAKOUT = "breakout"
    NEWS_DRIVEN = "news_driven"


@dataclass(frozen=True)
class MarketSnapshot:
    """Minimal market snapshot for regime classification."""
    symbol: str
    price: float
    open: float
    high: float
    low: float
    close: float
    volume: float
    avg_volume_20d: float
    atr_14: float
    atr_14_avg: float
    adx_14: float
    bb_width: float
    bb_width_avg: float
    prev_high: float
    prev_low: float
    prev_close: float
    session: str = "regular"
    gap_pct: float = 0.0


@dataclass(frozen=True)
class RegimeResult:
    primary: str
    secondary: List[str] = field(default_factory=list)
    confidence: float = 0.0
    reasons: List[str] = field(default_factory=list)


# ── Thresholds (verbatim from operator artifact) ────────────────────
VOL_LOW_RATIO = 0.7
VOL_HIGH_RATIO = 1.5
ADX_TREND_THRESHOLD = 25.0
ADX_CHOP_THRESHOLD = 20.0
SQUEEZE_BB_RATIO = 0.6
BREAKOUT_VOLUME_RATIO = 2.0
BREAKOUT_RANGE_PCT = 0.5
NEWS_GAP_PCT = 1.0
NEWS_VOLUME_RATIO = 3.0


def classify_regime(snapshot: MarketSnapshot) -> RegimeResult:
    """Classify market regime from snapshot. Same snapshot → same result."""
    reasons: List[str] = []
    candidates: dict[str, float] = {}

    # Volatility
    vol_ratio = (
        snapshot.atr_14 / snapshot.atr_14_avg
        if snapshot.atr_14_avg > 0 else 1.0
    )
    if vol_ratio < VOL_LOW_RATIO:
        candidates[Regime.LOW_VOL] = 1.0 - vol_ratio
        reasons.append(f"atr_low_ratio={vol_ratio:.2f}")
    elif vol_ratio > VOL_HIGH_RATIO:
        candidates[Regime.HIGH_VOL] = min(vol_ratio - 1.0, 1.0)
        reasons.append(f"atr_high_ratio={vol_ratio:.2f}")
    else:
        candidates[Regime.NORMAL] = 0.5
        reasons.append(f"atr_normal_ratio={vol_ratio:.2f}")

    # Trend
    if snapshot.adx_14 > ADX_TREND_THRESHOLD:
        if (snapshot.close > snapshot.open
                and snapshot.high > snapshot.prev_high):
            candidates[Regime.TREND_UP] = (
                (snapshot.adx_14 - ADX_TREND_THRESHOLD) / 25.0
            )
            reasons.append(f"adx_trend_up={snapshot.adx_14:.1f}")
        elif (snapshot.close < snapshot.open
                and snapshot.low < snapshot.prev_low):
            candidates[Regime.TREND_DOWN] = (
                (snapshot.adx_14 - ADX_TREND_THRESHOLD) / 25.0
            )
            reasons.append(f"adx_trend_down={snapshot.adx_14:.1f}")
    elif snapshot.adx_14 < ADX_CHOP_THRESHOLD:
        candidates[Regime.CHOP] = 1.0 - (snapshot.adx_14 / ADX_CHOP_THRESHOLD)
        reasons.append(f"adx_chop={snapshot.adx_14:.1f}")

    # Squeeze
    bb_ratio = (
        snapshot.bb_width / snapshot.bb_width_avg
        if snapshot.bb_width_avg > 0 else 1.0
    )
    if bb_ratio < SQUEEZE_BB_RATIO:
        candidates[Regime.SQUEEZE] = 1.0 - bb_ratio
        reasons.append(f"bb_squeeze_ratio={bb_ratio:.2f}")

    # Breakout
    daily_range_pct = (snapshot.high - snapshot.low) / snapshot.price * 100
    volume_ratio = (
        snapshot.volume / snapshot.avg_volume_20d
        if snapshot.avg_volume_20d > 0 else 1.0
    )
    if (volume_ratio > BREAKOUT_VOLUME_RATIO
            and daily_range_pct > BREAKOUT_RANGE_PCT):
        breakout_strength = (
            min(volume_ratio / 3.0, 1.0)
            * min(daily_range_pct / 2.0, 1.0)
        )
        candidates[Regime.BREAKOUT] = breakout_strength
        reasons.append(
            f"breakout_vol={volume_ratio:.1f}x range={daily_range_pct:.2f}%"
        )

    # News-driven
    if (abs(snapshot.gap_pct) > NEWS_GAP_PCT
            or volume_ratio > NEWS_VOLUME_RATIO):
        if snapshot.session != "regular":
            news_strength = max(
                abs(snapshot.gap_pct) / 2.0, volume_ratio / 4.0,
            )
            candidates[Regime.NEWS_DRIVEN] = min(news_strength, 1.0)
            reasons.append(
                f"news_gap={snapshot.gap_pct:.2f}% "
                f"vol={volume_ratio:.1f}x session={snapshot.session}"
            )

    # Resolve
    if not candidates:
        return RegimeResult(
            primary=Regime.NORMAL,
            secondary=[],
            confidence=0.5,
            reasons=["no_clear_signals_default_normal"],
        )

    sorted_candidates = sorted(
        candidates.items(), key=lambda x: x[1], reverse=True,
    )
    primary, primary_strength = sorted_candidates[0]
    secondary = [
        r for r, s in sorted_candidates[1:]
        if s > primary_strength * 0.3
    ]
    clarity_bonus = 1.0 - (len(secondary) * 0.1)
    confidence = min(primary_strength * clarity_bonus, 1.0)

    return RegimeResult(
        primary=primary,
        secondary=secondary,
        confidence=round(confidence, 4),
        reasons=reasons,
    )


def is_trending(result: RegimeResult) -> bool:
    return result.primary in (Regime.TREND_UP, Regime.TREND_DOWN)


def is_volatile(result: RegimeResult) -> bool:
    return result.primary in (
        Regime.HIGH_VOL, Regime.BREAKOUT, Regime.NEWS_DRIVEN,
    )


def is_range_bound(result: RegimeResult) -> bool:
    return result.primary in (
        Regime.CHOP, Regime.SQUEEZE, Regime.LOW_VOL,
    )
