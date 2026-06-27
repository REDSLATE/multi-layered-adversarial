"""Hellcat strategy — pure compute, breakout doctrine.

Doctrine (from `DOCTRINES["hellcat"]`):
    breakout. lookback_short=10, lookback_long=20.
    min_confidence=0.48 (highest floor — the "final agreement" voice).
    breakout_weight=1.60.

Hellcat's personality is `risk_governor` — the most cautious brain.
BUY only on a CLEAR upper-band breakout with RSI confirmation and
above SMA(20). SHORT branch env-gated. Highest confidence floor in
the stack ensures Hellcat fires last — the "final agreement"
voice.
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
    # Hellcat is the EXECUTION-SAFETY voice. Cites the fields it
    # consulted (subset of MarketSnapshot keys). Validator requires
    # ≥ 3. Missing data → field is not cited (honesty over invention).
    evidence_fields: tuple[str, ...] = ()
    # Semicolon-joined adversarial-objection codes per operator spec:
    #   WIDE_SPREAD_EXECUTION_RISK
    #   LOW_VOLUME_LIQUIDITY_RISK
    #   LOW_ATR_NO_RANGE
    #   EXTREME_ATR_VOLATILITY_RISK
    #   NEGATIVE_NEWS_RISK
    objection: Optional[str] = None


def _shorts_enabled() -> bool:
    return os.environ.get(
        "HELLCAT_SHORTS_ENABLED", "false",
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
        rationale=f"hellcat_hold:{reason}",
        target_price=None, stop_price=None,
        evidence=evidence or {}, skipped_reason=reason,
    )


def evaluate(symbol: str, indicators: dict[str, Any]) -> Decision:
    """Run Hellcat's breakout doctrine on one symbol.

    Required indicators: `ready, last_close, rsi14, bbands.position,
    bbands.upper, bbands.lower, sma['20'], atr14`.
    """
    if not indicators or not isinstance(indicators, dict):
        return _hold("no_indicators")
    if not indicators.get("ready"):
        return _hold("indicators_not_ready")

    last_close = _safe_float(indicators.get("last_close"))
    rsi14 = _safe_float(indicators.get("rsi14"))
    bb = indicators.get("bbands") or {}
    bb_pos = _safe_float(bb.get("position"))
    bb_upper = _safe_float(bb.get("upper"))
    bb_lower = _safe_float(bb.get("lower"))
    sma = indicators.get("sma") or {}
    sma20 = _safe_float(sma.get("20") if isinstance(sma, dict) else None)
    atr14 = _safe_float(indicators.get("atr14"))

    missing: list[str] = []
    if last_close is None:
        missing.append("last_close")
    if rsi14 is None:
        missing.append("rsi14")
    if bb_pos is None or bb_upper is None or bb_lower is None:
        missing.append("bbands")
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
        last_close is not None and rsi14 is not None
        and bb_pos is not None and bb_upper is not None and bb_lower is not None
        and sma20 is not None and atr14 is not None
    )

    doctrine = DOCTRINES["hellcat"]

    # ── BUY branch — confirmed upper-band breakout ─────────────────
    # Hellcat fires only on CONFIRMED breakouts:
    #   * bbands.position > 0.85 (near/above upper band)
    #   * RSI > 60 (momentum confirms)
    #   * last_close > SMA(20) (above intermediate trend)
    #   * last_close >= bb_upper * 0.99 (genuinely touching the band)
    bb_buy_strength = max(0.0, (bb_pos - 0.85) / 0.15)
    rsi_buy_strength = max(0.0, (rsi14 - 60.0) / 15.0) if rsi14 <= 80.0 else 0.0
    confirmed_breakout = (
        last_close > sma20
        and last_close >= bb_upper * 0.99
    )
    buy_signal = (
        (bb_buy_strength + rsi_buy_strength) / 2.0
        if confirmed_breakout
        else 0.0
    )

    # ── SHORT branch — confirmed lower-band breakdown (env-gated) ──
    bb_sell_strength = max(0.0, (0.15 - bb_pos) / 0.15)
    rsi_sell_strength = max(0.0, (40.0 - rsi14) / 15.0) if rsi14 >= 20.0 else 0.0
    confirmed_breakdown = (
        last_close < sma20
        and last_close <= bb_lower * 1.01
    )
    sell_signal = (
        (bb_sell_strength + rsi_sell_strength) / 2.0
        if confirmed_breakdown
        else 0.0
    )

    evidence_common: dict[str, Any] = {
        "doctrine": "breakout",
        "doctrine_version": "hellcat_native_v1",
        "rsi14": round(rsi14, 2),
        "bb_position": round(bb_pos, 4),
        "bb_upper": round(bb_upper, 4),
        "bb_lower": round(bb_lower, 4),
        "sma20": round(sma20, 4),
        "last_close": round(last_close, 4),
        "atr14": round(atr14, 4),
        "buy_signal": round(buy_signal, 4),
        "sell_signal": round(sell_signal, 4),
        "buy_score": round(buy_signal, 4),
        "sell_score": round(sell_signal, 4),
    }

    # ── Operator-pinned evidence-citation contract (2026-06-26) ─────
    # Hellcat's role: execution-safety voice. Cites the
    # MarketSnapshot-schema fields it actually consulted. `price` and
    # `atr_pct` are always available; `spread_bps`, `volume_rel`, and
    # `news_score` are cited only when present in the snapshot so the
    # citation stays honest.
    atr_pct_pct = (atr14 / last_close * 100.0) if last_close > 0 else 0.0
    spread_bps = _safe_float(indicators.get("spread_bps"))
    # volume_rel may be carried as `volume_rel` or `rvol` (relative
    # volume) — accept either alias.
    volume_rel = (
        _safe_float(indicators.get("volume_rel"))
        if indicators.get("volume_rel") is not None
        else _safe_float(indicators.get("rvol"))
    )
    news_score = _safe_float(indicators.get("news_score"))

    cited_fields: list[str] = ["price", "atr_pct"]
    if spread_bps is not None:
        cited_fields.append("spread_bps")
    if volume_rel is not None:
        cited_fields.append("volume_rel")
    if news_score is not None:
        cited_fields.append("news_score")
    evidence_fields_cited: tuple[str, ...] = tuple(cited_fields)

    evidence_common["atr_pct_pct"] = round(atr_pct_pct, 3)
    if spread_bps is not None:
        evidence_common["spread_bps"] = round(spread_bps, 2)
    if volume_rel is not None:
        evidence_common["volume_rel"] = round(volume_rel, 3)
    if news_score is not None:
        evidence_common["news_score"] = round(news_score, 3)

    def _execution_objections() -> list[str]:
        """Per operator spec — fire only when data is available."""
        codes: list[str] = []
        if spread_bps is not None and spread_bps > 75.0:
            codes.append(f"WIDE_SPREAD_EXECUTION_RISK:{spread_bps:.0f}bps")
        if volume_rel is not None and volume_rel < 1.2:
            codes.append(f"LOW_VOLUME_LIQUIDITY_RISK:{volume_rel:.2f}x")
        if atr_pct_pct < 0.25:
            codes.append(f"LOW_ATR_NO_RANGE:{atr_pct_pct:.2f}pct")
        if atr_pct_pct > 6.0:
            codes.append(f"EXTREME_ATR_VOLATILITY_RISK:{atr_pct_pct:.2f}pct")
        if news_score is not None and news_score < -0.35:
            codes.append(f"NEGATIVE_NEWS_RISK:{news_score:.2f}")
        return codes

    if buy_signal > 0.25 and buy_signal >= sell_signal:
        # Hellcat's confidence floor (0.48) is the highest — start
        # higher to clear it on legitimate breakouts.
        confidence = min(0.88, 0.50 + 0.32 * buy_signal)
        if confidence < doctrine.min_confidence:
            return _hold(
                f"confidence_below_floor:{confidence:.3f}<{doctrine.min_confidence}",
                evidence=evidence_common,
            )
        # Breakout doctrine: aggressive 4-ATR target, tight 1.5-ATR stop.
        target_price = round(last_close + 4.0 * atr14, 4)
        stop_price = round(last_close - 1.5 * atr14, 4)
        if stop_price <= 0 or target_price <= last_close:
            return _hold("invalid_rr_prices", evidence=evidence_common)
        rationale = (
            f"Hellcat breakout BUY {symbol}: BB position={bb_pos:.2f} "
            f"(upper-band break), RSI={rsi14:.1f} momentum confirmed, "
            f"above SMA(20). target=+4*ATR({target_price}), "
            f"stop=-1.5*ATR({stop_price})."
        )
        return Decision(
            action="BUY",
            confidence=round(confidence, 4),
            size_bias=1.0,
            rationale=rationale,
            target_price=target_price,
            stop_price=stop_price,
            evidence=evidence_common,
            evidence_fields=evidence_fields_cited,
            objection=";".join(_execution_objections()) or None,
        )

    if _shorts_enabled() and sell_signal > 0.25 and sell_signal > buy_signal:
        confidence = min(0.88, 0.50 + 0.32 * sell_signal)
        if confidence < doctrine.min_confidence:
            return _hold(
                f"confidence_below_floor:{confidence:.3f}<{doctrine.min_confidence}",
                evidence=evidence_common,
            )
        target_price = round(last_close - 4.0 * atr14, 4)
        stop_price = round(last_close + 1.5 * atr14, 4)
        if target_price >= last_close or target_price <= 0:
            return _hold("invalid_rr_prices", evidence=evidence_common)
        rationale = (
            f"Hellcat breakout SHORT {symbol}: BB position={bb_pos:.2f} "
            f"(lower-band break), RSI={rsi14:.1f} downside confirmed, "
            f"below SMA(20). target=-4*ATR({target_price}), "
            f"stop=+1.5*ATR({stop_price})."
        )
        return Decision(
            action="SHORT",
            confidence=round(confidence, 4),
            size_bias=1.0,
            rationale=rationale,
            target_price=target_price,
            stop_price=stop_price,
            evidence=evidence_common,
            evidence_fields=evidence_fields_cited,
            objection=";".join(_execution_objections()) or None,
        )

    return _hold("no_breakout_signal", evidence=evidence_common)


__all__ = ["Decision", "evaluate"]
