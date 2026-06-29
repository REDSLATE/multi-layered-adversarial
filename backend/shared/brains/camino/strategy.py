"""Camino strategy — pure compute, trend doctrine.

Doctrine (from `DOCTRINES["camino"]`):
    trend. lookback_short=20, lookback_long=50.
    min_confidence=0.46, min_gap=0.08. trend_weight=1.40.

Camino's personality is `opportunity_hunter` — surfaces high-conviction
trend continuation setups. BUY when an unambiguous uptrend is in
force (price > SMA20 > SMA50 with RSI in the healthy band). SHORT
branch env-gated.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Literal, Optional

from shared.brain_doctrine import DOCTRINES
from shared.brains._doctrine_overrides import effective_min_confidence


Action = Literal["BUY", "SHORT", "HOLD"]


@dataclass(frozen=True)
class Decision:
    action: Action
    confidence: float
    size_bias: float
    rationale: str
    target_price: Optional[float]
    stop_price: Optional[float]
    evidence: dict[str, Any] = field(default_factory=dict)
    skipped_reason: Optional[str] = None
    # 2026-02-25 — `_runner_core._build_intent_body` reads these on
    # every emission. Other brains (barracuda/gto/hellcat) defined
    # them; Camino was missing them, so every Camino tick crashed
    # with `AttributeError: 'Decision' object has no attribute
    # 'evidence_fields'` (visible in prod backend logs Jun 29 15:11).
    # Default-empty preserves Camino's current "no cited evidence"
    # behavior — when Camino learns to cite (P2 backlog item from
    # 2026-02-23) it can populate them.
    evidence_fields: tuple = ()
    objection: Optional[str] = None


def _shorts_enabled() -> bool:
    return os.environ.get(
        "CAMINO_SHORTS_ENABLED", "false",
    ).strip().lower() in {"1", "true", "yes", "on"}


def _safe_float(v: Any) -> Optional[float]:
    try:
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _hold(reason: str, evidence: dict[str, Any] | None = None) -> Decision:
    return Decision(
        action="HOLD", confidence=0.0, size_bias=0.0,
        rationale=f"camino_hold:{reason}",
        target_price=None, stop_price=None,
        evidence=evidence or {}, skipped_reason=reason,
    )


def evaluate(symbol: str, indicators: dict[str, Any]) -> Decision:
    """Run Camino's trend doctrine on one symbol.

    Required indicators: `ready, last_close, rsi14, sma['20'],
    sma['50'], ema['12'], atr14`.
    """
    if not indicators or not isinstance(indicators, dict):
        return _hold("no_indicators")
    if not indicators.get("ready"):
        return _hold("indicators_not_ready")

    last_close = _safe_float(indicators.get("last_close"))
    rsi14 = _safe_float(indicators.get("rsi14"))
    sma = indicators.get("sma") or {}
    sma20 = _safe_float(sma.get("20") if isinstance(sma, dict) else None)
    sma50 = _safe_float(sma.get("50") if isinstance(sma, dict) else None)
    ema = indicators.get("ema") or {}
    ema12 = _safe_float(ema.get("12") if isinstance(ema, dict) else None)
    atr14 = _safe_float(indicators.get("atr14"))

    missing: list[str] = []
    if last_close is None:
        missing.append("last_close")
    if rsi14 is None:
        missing.append("rsi14")
    if sma20 is None or sma50 is None:
        missing.append("sma")
    if ema12 is None:
        missing.append("ema12")
    if atr14 is None or atr14 <= 0:
        missing.append("atr14")
    if missing:
        return _hold(
            "missing_indicators:" + ",".join(missing),
            evidence={"missing": missing},
        )
    assert (
        last_close is not None and rsi14 is not None and sma20 is not None
        and sma50 is not None and ema12 is not None and atr14 is not None
    )

    doctrine = DOCTRINES["camino"]
    # 2026-02-25 — read operator UI override (placebo bug fix).
    min_conf = effective_min_confidence(doctrine, lane="equity")

    # ── BUY branch — confirmed uptrend continuation ────────────────
    # last_close > SMA(20) > SMA(50), RSI in 45..70 (healthy), and
    # price within striking distance of EMA(12) (not extended).
    uptrend = last_close > sma20 > sma50
    rsi_healthy_long = 45.0 <= rsi14 <= 70.0
    near_ema12_long = last_close <= ema12 * 1.04  # within +4% of EMA12

    # Strength: how far above the 20-SMA we are vs the 20→50 slope
    sma_slope_pct = (sma20 - sma50) / sma50 if sma50 > 0 else 0.0
    trend_strength = max(0.0, min(1.0, sma_slope_pct * 25.0))  # 4% slope=1.0
    rsi_band_strength = max(0.0, (rsi14 - 45.0) / 25.0) if rsi14 <= 70.0 else 0.0
    buy_signal = (
        (trend_strength + rsi_band_strength) / 2.0
        if (uptrend and rsi_healthy_long and near_ema12_long)
        else 0.0
    )

    # ── SHORT branch — confirmed downtrend (env-gated) ─────────────
    downtrend = last_close < sma20 < sma50
    rsi_healthy_short = 30.0 <= rsi14 <= 55.0
    near_ema12_short = last_close >= ema12 * 0.96
    sma_slope_pct_neg = (sma50 - sma20) / sma50 if sma50 > 0 else 0.0
    trend_strength_short = max(0.0, min(1.0, sma_slope_pct_neg * 25.0))
    rsi_band_strength_short = max(0.0, (55.0 - rsi14) / 25.0) if rsi14 >= 30.0 else 0.0
    sell_signal = (
        (trend_strength_short + rsi_band_strength_short) / 2.0
        if (downtrend and rsi_healthy_short and near_ema12_short)
        else 0.0
    )

    evidence_common: dict[str, Any] = {
        "doctrine": "trend",
        "doctrine_version": "camino_native_v1",
        "rsi14": round(rsi14, 2),
        "sma20": round(sma20, 4),
        "sma50": round(sma50, 4),
        "ema12": round(ema12, 4),
        "last_close": round(last_close, 4),
        "atr14": round(atr14, 4),
        "buy_signal": round(buy_signal, 4),
        "sell_signal": round(sell_signal, 4),
        "buy_score": round(buy_signal, 4),
        "sell_score": round(sell_signal, 4),
    }

    if buy_signal > 0.20 and buy_signal >= sell_signal:
        confidence = min(0.85, 0.47 + 0.30 * buy_signal)
        if confidence < min_conf:
            return _hold(
                f"confidence_below_floor:{confidence:.3f}<{min_conf}",
                evidence=evidence_common,
            )
        # Trend doctrine targets 2.5 ATR rally; trailing 2 ATR stop.
        target_price = round(last_close + 2.5 * atr14, 4)
        stop_price = round(last_close - 2.0 * atr14, 4)
        if stop_price <= 0 or target_price <= last_close:
            return _hold("invalid_rr_prices", evidence=evidence_common)
        rationale = (
            f"Camino trend BUY {symbol}: SMA(20)>SMA(50) uptrend, "
            f"RSI={rsi14:.1f} healthy. target=+2.5*ATR({target_price}), "
            f"stop=-2*ATR({stop_price})."
        )
        return Decision(
            action="BUY",
            confidence=round(confidence, 4),
            size_bias=1.0,
            rationale=rationale,
            target_price=target_price,
            stop_price=stop_price,
            evidence=evidence_common,
        )

    if _shorts_enabled() and sell_signal > 0.20 and sell_signal > buy_signal:
        confidence = min(0.85, 0.47 + 0.30 * sell_signal)
        if confidence < min_conf:
            return _hold(
                f"confidence_below_floor:{confidence:.3f}<{min_conf}",
                evidence=evidence_common,
            )
        target_price = round(last_close - 2.5 * atr14, 4)
        stop_price = round(last_close + 2.0 * atr14, 4)
        if target_price >= last_close or target_price <= 0:
            return _hold("invalid_rr_prices", evidence=evidence_common)
        rationale = (
            f"Camino trend SHORT {symbol}: SMA(20)<SMA(50) downtrend, "
            f"RSI={rsi14:.1f}. target=-2.5*ATR({target_price}), "
            f"stop=+2*ATR({stop_price})."
        )
        return Decision(
            action="SHORT",
            confidence=round(confidence, 4),
            size_bias=1.0,
            rationale=rationale,
            target_price=target_price,
            stop_price=stop_price,
            evidence=evidence_common,
        )

    return _hold("no_trend_signal", evidence=evidence_common)


__all__ = ["Decision", "evaluate"]
