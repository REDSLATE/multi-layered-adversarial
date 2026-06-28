"""Barracuda strategy — pure compute, no I/O.

Doctrine (from `shared.brain_doctrine.DOCTRINES["barracuda"]`):
    mean_reversion. lookback_short=14, lookback_long=30.
    min_confidence=0.43, min_gap=0.06. mean_reversion_weight=1.50.

This module is the *interpretation* layer. It receives a normalized
indicator snapshot for one symbol and returns a `Decision` describing
whether Barracuda's mean-reversion doctrine would fire BUY / SHORT or
HOLD, with confidence, R:R prices, rationale, and audit evidence.

It does NOT:
  * touch the DB (the runner does that)
  * call an LLM
  * emit to /api/intents (the runner does that)
  * decide whether the brain holds the executor seat (MC does that)
  * apply legacy wrappers (the canonical emit path does that)

It is intentionally deterministic and side-effect free so unit tests
can exercise the entire decision tree without any fixtures beyond a
plain `indicators` dict.

v1 emits BUY only (long mean-reversion on oversold equity). SHORT is
gated behind `BARRACUDA_SHORTS_ENABLED=true` env so the operator can
observe a long-only baseline before the doctrine starts shorting.
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
    skipped_reason: Optional[str] = None  # populated for HOLDs
    # ── Operator doctrine 2026-06-26: evidence-citation contract ────
    # Subset of `MarketSnapshot.__annotations__` keys the brain
    # consulted. Validator (`consensus_evidence.validate_opinion`)
    # requires ≥ 3 valid names. Defaults to `()` so HOLDs/legacy
    # callers keep working.
    evidence_fields: tuple[str, ...] = ()
    # Semicolon-joined adversarial-objection codes (e.g.
    # "RSI_NOT_OVERSOLD;PRICE_NEAR_UPPER_BAND"). None when Barracuda
    # is taking a clean BUY/SHORT setup with no flags.
    objection: Optional[str] = None


def _shorts_enabled() -> bool:
    return os.environ.get(
        "BARRACUDA_SHORTS_ENABLED", "false",
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
        action="HOLD",
        confidence=0.0,
        size_bias=0.0,
        rationale=f"barracuda_hold:{reason}",
        target_price=None,
        stop_price=None,
        evidence=evidence or {},
        skipped_reason=reason,
    )


def evaluate(symbol: str, indicators: dict[str, Any]) -> Decision:
    """Run Barracuda's mean-reversion doctrine on a single symbol.

    `indicators` is the `indicators` sub-dict of a
    `shared_indicator_snapshots` row. Required keys (any missing →
    HOLD with a typed skipped_reason):

        last_close            : float
        rsi14                 : float
        bbands.position       : float in [0, 1]   (0 = at lower band)
        bbands.mid            : float             (target for BUY)
        sma['20'], sma['50']  : float             (trend filter)
        atr14                 : float             (stop sizing)
    """
    if not indicators or not isinstance(indicators, dict):
        return _hold("no_indicators")

    if not indicators.get("ready"):
        return _hold("indicators_not_ready")

    last_close = _safe_float(indicators.get("last_close"))
    rsi14 = _safe_float(indicators.get("rsi14"))
    bb = indicators.get("bbands") or {}
    bb_pos = _safe_float(bb.get("position"))
    bb_mid = _safe_float(bb.get("mid"))
    sma = indicators.get("sma") or {}
    sma20 = _safe_float(sma.get("20") if isinstance(sma, dict) else None)
    sma50 = _safe_float(sma.get("50") if isinstance(sma, dict) else None)
    atr14 = _safe_float(indicators.get("atr14"))

    missing: list[str] = []
    if last_close is None:
        missing.append("last_close")
    if rsi14 is None:
        missing.append("rsi14")
    if bb_pos is None or bb_mid is None:
        missing.append("bbands")
    if sma20 is None or sma50 is None:
        missing.append("sma")
    if atr14 is None or atr14 <= 0:
        missing.append("atr14")
    if missing:
        return _hold(
            "missing_indicators:" + ",".join(missing),
            evidence={"missing": missing},
        )

    # mypy/lint comfort — None branches already handled above
    assert last_close is not None and rsi14 is not None
    assert bb_pos is not None and bb_mid is not None
    assert sma20 is not None and sma50 is not None
    assert atr14 is not None

    doctrine = DOCTRINES["barracuda"]
    # 2026-02-25 — read operator UI override (placebo bug fix). Falls
    # back to doctrine default when no override is set.
    min_conf = effective_min_confidence(doctrine, lane="equity")

    # ── BUY branch — oversold mean-reversion long ──────────────────
    # Triggers: RSI < 35, BB position < 0.25, price still within
    # broader uptrend filter (above 92% of 50-SMA).
    rsi_buy_strength = max(0.0, (35.0 - rsi14) / 35.0)        # 0..1
    bb_buy_strength = max(0.0, (0.25 - bb_pos) / 0.25)        # 0..1
    buy_signal = (rsi_buy_strength + bb_buy_strength) / 2.0   # 0..1
    in_buy_trend = last_close > (sma50 * 0.92)

    # ── SHORT branch — overbought mean-reversion (env-gated) ───────
    rsi_sell_strength = max(0.0, (rsi14 - 65.0) / 35.0)
    bb_sell_strength = max(0.0, (bb_pos - 0.75) / 0.25)
    sell_signal = (rsi_sell_strength + bb_sell_strength) / 2.0
    in_sell_trend = last_close < (sma50 * 1.08)

    evidence_common: dict[str, Any] = {
        "doctrine": "mean_reversion",
        "doctrine_version": "barracuda_native_v1",
        "rsi14": round(rsi14, 2),
        "bb_position": round(bb_pos, 4),
        "bb_mid": round(bb_mid, 4),
        "last_close": round(last_close, 4),
        "sma20": round(sma20, 4),
        "sma50": round(sma50, 4),
        "atr14": round(atr14, 4),
        "buy_signal": round(buy_signal, 4),
        "sell_signal": round(sell_signal, 4),
        # Tape-score surface so the legacy Camaro wrapper's HOLD-
        # rescue branch reads from the same lens (buy/sell_score).
        "buy_score": round(rsi_buy_strength, 4),
        "sell_score": round(rsi_sell_strength, 4),
    }

    # ── Operator-pinned evidence-citation contract (2026-06-26) ─────
    # Barracuda always inspects the same MarketSnapshot fields:
    #   rsi          ← rsi14
    #   atr_pct      ← atr14
    #   trend_1h     ← close-to-sma50 trend slope (proxy)
    # These three are constant across every Barracuda emission. The
    # operator's spec requires ≥ 3 cited MarketSnapshot keys, so this
    # is the minimum honest disclosure of what Barracuda actually
    # consulted. Other brains will cite their own field sets.
    evidence_fields_cited: tuple[str, ...] = ("rsi", "atr_pct", "trend_1h")

    # Build the adversarial-objection set from the same signals — i.e.
    # Barracuda's reasons to be CAUTIOUS even if it's about to issue a
    # BUY. These are surfaced to the consensus engine so a same-side
    # opinion that includes objections still earns full weight.
    objection_codes: list[str] = []
    # RSI sanity — mean-reversion BUY wants RSI deeply oversold; the
    # closer to neutral, the weaker the edge.
    if rsi14 > 30.0:
        objection_codes.append(f"RSI_NOT_DEEPLY_OVERSOLD:{rsi14:.1f}")
    # BB position — should be near lower band for a long-reversion entry.
    if bb_pos > 0.30:
        objection_codes.append(f"PRICE_NOT_AT_LOWER_BAND:{bb_pos:.2f}")
    # ATR sanity — a flat tape gives no reversion edge to capture.
    atr_pct = (atr14 / last_close) if last_close > 0 else 0.0
    if atr_pct < 0.005:
        objection_codes.append(f"ATR_TOO_LOW:{atr_pct * 100:.2f}pct")
    # Trend filter — fading a strong downtrend is dangerous.
    trend_slope = ((last_close - sma50) / sma50) if sma50 > 0 else 0.0
    if trend_slope < -0.05:
        objection_codes.append(f"TREND_BELOW_SMA50:{trend_slope * 100:.1f}pct")

    # BUY wins if buy_signal > sell_signal AND clears the floor.
    if buy_signal > 0.20 and buy_signal >= sell_signal and in_buy_trend:
        # confidence: 0.45..0.75, scaled by doctrine.mean_reversion_weight.
        confidence = 0.45 + 0.30 * buy_signal
        confidence = min(0.85, confidence)
        if confidence < min_conf:
            return _hold(
                f"confidence_below_floor:{confidence:.3f}<{min_conf}",
                evidence=evidence_common,
            )
        # R:R prices — target the BB mid; stop = entry - 2*ATR.
        target_price = round(bb_mid, 4)
        stop_price = round(last_close - 2.0 * atr14, 4)
        if stop_price <= 0:
            return _hold("invalid_stop_price", evidence=evidence_common)
        if target_price <= last_close:
            return _hold("target_not_above_entry", evidence=evidence_common)
        size_bias = 1.0  # canonical wrapper layer applies dampener
        rationale = (
            f"Barracuda mean-reversion BUY {symbol}: RSI={rsi14:.1f} "
            f"oversold, BB position={bb_pos:.2f} (lower-band tag), "
            f"target=mid({target_price}), stop=2*ATR({stop_price})."
        )
        return Decision(
            action="BUY",
            confidence=round(confidence, 4),
            size_bias=size_bias,
            rationale=rationale,
            target_price=target_price,
            stop_price=stop_price,
            evidence=evidence_common,
            evidence_fields=evidence_fields_cited,
            objection=";".join(objection_codes) or None,
        )

    if (
        _shorts_enabled()
        and sell_signal > 0.20
        and sell_signal > buy_signal
        and in_sell_trend
    ):
        confidence = 0.45 + 0.30 * sell_signal
        confidence = min(0.85, confidence)
        if confidence < min_conf:
            return _hold(
                f"confidence_below_floor:{confidence:.3f}<{min_conf}",
                evidence=evidence_common,
            )
        target_price = round(bb_mid, 4)
        stop_price = round(last_close + 2.0 * atr14, 4)
        if target_price >= last_close:
            return _hold("target_not_below_entry", evidence=evidence_common)
        rationale = (
            f"Barracuda mean-reversion SHORT {symbol}: RSI={rsi14:.1f} "
            f"overbought, BB position={bb_pos:.2f} (upper-band tag), "
            f"target=mid({target_price}), stop=2*ATR({stop_price})."
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
            objection=";".join(objection_codes) or None,
        )

    return _hold(
        "no_mean_reversion_signal",
        evidence=evidence_common,
    )


__all__ = ["Decision", "evaluate"]
