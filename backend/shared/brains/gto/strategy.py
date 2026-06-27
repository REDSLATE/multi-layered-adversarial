"""GTO strategy — pure compute, momentum doctrine.

Doctrine (from `DOCTRINES["gto"]`):
    momentum. lookback_short=8, lookback_long=21.
    min_confidence=0.45, min_gap=0.07. momentum_weight=1.60.

GTO's classical personality is `adversarial_short_hunter` — looks for
downside / trap / short opportunities. In v1 we keep BUY emissions
on (so the brain participates in the consensus pool) but the BUY
threshold is tighter than the SHORT threshold, mirroring the
adversarial bias. SHORT branch env-gated.

Signal: MACD histogram + RSI + EMA(12)/EMA(26) trend confirmation.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Literal, Optional

from shared.brain_doctrine import DOCTRINES


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
    # ── Operator doctrine 2026-06-26: evidence-citation contract ────
    # Subset of `MarketSnapshot.__annotations__` keys consulted.
    # GTO's momentum doctrine reads short/long trend, RSI, and ATR%.
    # `volume_rel` is in the spec but not yet in GTO's indicator pull
    # — added once the data layer surfaces it.
    evidence_fields: tuple[str, ...] = ()
    # Semicolon-joined adversarial-objection codes
    # (e.g. "TREND_5M_DOWN;RSI_NOT_MOMENTUM"). None when momentum
    # confirmation is clean.
    objection: Optional[str] = None


def _shorts_enabled() -> bool:
    return os.environ.get(
        "GTO_SHORTS_ENABLED", "false",
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
        rationale=f"gto_hold:{reason}",
        target_price=None, stop_price=None,
        evidence=evidence or {}, skipped_reason=reason,
    )


def evaluate(symbol: str, indicators: dict[str, Any]) -> Decision:
    """Run GTO's momentum doctrine on one symbol.

    Required indicator keys: `ready, last_close, rsi14, macd.hist,
    ema['12'], ema['26'], sma['20'], atr14`.
    """
    if not indicators or not isinstance(indicators, dict):
        return _hold("no_indicators")
    if not indicators.get("ready"):
        return _hold("indicators_not_ready")

    last_close = _safe_float(indicators.get("last_close"))
    rsi14 = _safe_float(indicators.get("rsi14"))
    macd = indicators.get("macd") or {}
    macd_hist = _safe_float(macd.get("hist"))
    ema = indicators.get("ema") or {}
    ema12 = _safe_float(ema.get("12") if isinstance(ema, dict) else None)
    ema26 = _safe_float(ema.get("26") if isinstance(ema, dict) else None)
    sma = indicators.get("sma") or {}
    sma20 = _safe_float(sma.get("20") if isinstance(sma, dict) else None)
    atr14 = _safe_float(indicators.get("atr14"))

    missing: list[str] = []
    if last_close is None:
        missing.append("last_close")
    if rsi14 is None:
        missing.append("rsi14")
    if macd_hist is None:
        missing.append("macd_hist")
    if ema12 is None or ema26 is None:
        missing.append("ema")
    if sma20 is None:
        missing.append("sma20")
    if atr14 is None or atr14 <= 0:
        missing.append("atr14")
    if missing:
        return _hold(
            "missing_indicators:" + ",".join(missing),
            evidence={"missing": missing},
        )
    assert (
        last_close is not None and rsi14 is not None and macd_hist is not None
        and ema12 is not None and ema26 is not None
        and sma20 is not None and atr14 is not None
    )

    doctrine = DOCTRINES["gto"]
    atr_pct = atr14 / last_close if last_close > 0 else 0.0  # noqa: F841

    # ── BUY branch — confirmed upside momentum ─────────────────────
    # Tight (adversarial bias against blind longs):
    #   * MACD histogram > 0 (bullish)
    #   * RSI in 55..72 (momentum without overbought)
    #   * EMA12 > EMA26 (short trend > long trend)
    #   * last_close > SMA20 (above intermediate trend)
    macd_pct = abs(macd_hist) / last_close if last_close > 0 else 0.0
    macd_strength = min(1.0, macd_pct * 200.0)  # 0.005% ≈ 1.0 strength
    rsi_buy_strength = max(0.0, (rsi14 - 55.0) / 17.0) if rsi14 <= 72.0 else 0.0
    ema_trend_up = ema12 > ema26
    above_intermediate_trend = last_close > sma20

    buy_signal = (
        (macd_strength + rsi_buy_strength) / 2.0
        if (macd_hist > 0 and ema_trend_up and above_intermediate_trend)
        else 0.0
    )

    # ── SHORT branch — confirmed downside (env-gated) ──────────────
    rsi_sell_strength = max(0.0, (45.0 - rsi14) / 17.0) if rsi14 >= 28.0 else 0.0
    ema_trend_down = ema26 > ema12
    below_intermediate_trend = last_close < sma20
    sell_signal = (
        (macd_strength + rsi_sell_strength) / 2.0
        if (macd_hist < 0 and ema_trend_down and below_intermediate_trend)
        else 0.0
    )

    evidence_common: dict[str, Any] = {
        "doctrine": "momentum",
        "doctrine_version": "gto_native_v1",
        "rsi14": round(rsi14, 2),
        "macd_hist": round(macd_hist, 6),
        "ema12": round(ema12, 4),
        "ema26": round(ema26, 4),
        "sma20": round(sma20, 4),
        "last_close": round(last_close, 4),
        "atr14": round(atr14, 4),
        "buy_signal": round(buy_signal, 4),
        "sell_signal": round(sell_signal, 4),
        "buy_score": round(buy_signal, 4),
        "sell_score": round(sell_signal, 4),
    }

    # ── Operator-pinned evidence-citation contract (2026-06-26) ─────
    # GTO's momentum doctrine consults short trend (close vs EMA12),
    # long trend (close vs EMA26), RSI for momentum strength, and ATR%
    # for volatility floor. These four are constant per emission.
    evidence_fields_cited: tuple[str, ...] = (
        "trend_5m", "trend_1h", "rsi", "atr_pct",
    )

    # Compute the citation-friendly metrics used by objection rules.
    trend_5m_pct = ((last_close - ema12) / ema12) if ema12 > 0 else 0.0
    trend_1h_pct = ((last_close - ema26) / ema26) if ema26 > 0 else 0.0
    atr_pct_pct = ((atr14 / last_close) * 100.0) if last_close > 0 else 0.0
    evidence_common["trend_5m_pct"] = round(trend_5m_pct * 100.0, 3)
    evidence_common["trend_1h_pct"] = round(trend_1h_pct * 100.0, 3)
    evidence_common["atr_pct_pct"] = round(atr_pct_pct, 3)

    if buy_signal > 0.25 and buy_signal >= sell_signal:
        confidence = min(0.85, 0.46 + 0.30 * buy_signal)
        if confidence < doctrine.min_confidence:
            return _hold(
                f"confidence_below_floor:{confidence:.3f}<{doctrine.min_confidence}",
                evidence=evidence_common,
            )
        # Momentum doctrine targets a 3-ATR rally; stops 1.5 ATR below.
        target_price = round(last_close + 3.0 * atr14, 4)
        stop_price = round(last_close - 1.5 * atr14, 4)
        if stop_price <= 0:
            return _hold("invalid_stop_price", evidence=evidence_common)
        if target_price <= last_close:
            return _hold("target_not_above_entry", evidence=evidence_common)
        rationale = (
            f"GTO momentum BUY {symbol}: MACD hist={macd_hist:.4f} bullish, "
            f"RSI={rsi14:.1f}, EMA(12)>EMA(26), above SMA(20). "
            f"target=+3*ATR({target_price}), stop=-1.5*ATR({stop_price})."
        )
        # Operator-spec objection rules for BUY (momentum confirmation):
        #   trend_5m ≤ 0, trend_1h ≤ 0, rsi < 50, atr_pct < 0.4
        objection_codes_buy: list[str] = []
        if trend_5m_pct <= 0:
            objection_codes_buy.append(
                f"TREND_5M_NOT_UP:{trend_5m_pct * 100:.2f}pct"
            )
        if trend_1h_pct <= 0:
            objection_codes_buy.append(
                f"TREND_1H_NOT_UP:{trend_1h_pct * 100:.2f}pct"
            )
        if rsi14 < 50:
            objection_codes_buy.append(f"RSI_BELOW_MOMENTUM:{rsi14:.1f}")
        if atr_pct_pct < 0.4:
            objection_codes_buy.append(f"ATR_TOO_LOW:{atr_pct_pct:.2f}pct")
        return Decision(
            action="BUY",
            confidence=round(confidence, 4),
            size_bias=1.0,
            rationale=rationale,
            target_price=target_price,
            stop_price=stop_price,
            evidence=evidence_common,
            evidence_fields=evidence_fields_cited,
            objection=";".join(objection_codes_buy) or None,
        )

    if _shorts_enabled() and sell_signal > 0.25 and sell_signal > buy_signal:
        confidence = min(0.85, 0.46 + 0.30 * sell_signal)
        if confidence < doctrine.min_confidence:
            return _hold(
                f"confidence_below_floor:{confidence:.3f}<{doctrine.min_confidence}",
                evidence=evidence_common,
            )
        target_price = round(last_close - 3.0 * atr14, 4)
        stop_price = round(last_close + 1.5 * atr14, 4)
        if target_price >= last_close or target_price <= 0:
            return _hold("invalid_target_price", evidence=evidence_common)
        rationale = (
            f"GTO momentum SHORT {symbol}: MACD hist={macd_hist:.4f} bearish, "
            f"RSI={rsi14:.1f}, EMA(26)>EMA(12), below SMA(20). "
            f"target=-3*ATR({target_price}), stop=+1.5*ATR({stop_price})."
        )
        # Operator-spec objection rules for SELL/SHORT (symmetric):
        #   trend_5m ≥ 0, trend_1h ≥ 0, rsi > 50, atr_pct < 0.4
        objection_codes_sell: list[str] = []
        if trend_5m_pct >= 0:
            objection_codes_sell.append(
                f"TREND_5M_NOT_DOWN:{trend_5m_pct * 100:.2f}pct"
            )
        if trend_1h_pct >= 0:
            objection_codes_sell.append(
                f"TREND_1H_NOT_DOWN:{trend_1h_pct * 100:.2f}pct"
            )
        if rsi14 > 50:
            objection_codes_sell.append(f"RSI_ABOVE_MOMENTUM:{rsi14:.1f}")
        if atr_pct_pct < 0.4:
            objection_codes_sell.append(f"ATR_TOO_LOW:{atr_pct_pct:.2f}pct")
        return Decision(
            action="SHORT",
            confidence=round(confidence, 4),
            size_bias=1.0,
            rationale=rationale,
            target_price=target_price,
            stop_price=stop_price,
            evidence=evidence_common,
            evidence_fields=evidence_fields_cited,
            objection=";".join(objection_codes_sell) or None,
        )

    return _hold("no_momentum_signal", evidence=evidence_common)


__all__ = ["Decision", "evaluate"]
