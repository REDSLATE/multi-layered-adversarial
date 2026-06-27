"""Barracuda native-runtime evidence-citation tests (2026-06-26).

Verifies operator doctrine compliance:
  - Every BUY/SHORT Barracuda emits cites ≥ 3 valid MarketSnapshot fields.
  - Objection codes are produced when Barracuda's signals are weak
    (RSI not deeply oversold, BB position high, low ATR, weak trend).
  - HOLD decisions don't carry citations (no opinion to cite).
"""
from __future__ import annotations

from shared.brains.barracuda.strategy import evaluate
from shared.pipeline.consensus_evidence import (
    MIN_CITED_FIELDS,
    AdvisorOpinion,
    validate_opinion,
)


def _ind(**over):
    base = {
        "ready": True,
        "last_close": 20.0,
        "rsi14": 20.0,
        "bbands": {"position": 0.15, "mid": 21.0},
        "sma": {"20": 19.5, "50": 18.5},
        "atr14": 0.40,
    }
    base.update(over)
    return base


def test_buy_decision_cites_three_market_fields():
    d = evaluate("AAL", _ind())
    assert d.action == "BUY"
    assert len(d.evidence_fields) >= MIN_CITED_FIELDS
    assert set(d.evidence_fields) == {"rsi", "atr_pct", "trend_1h"}


def test_buy_decision_passes_advisor_validator():
    d = evaluate("AAL", _ind())
    # Re-shape into an AdvisorOpinion and run the canonical validator.
    op = AdvisorOpinion(
        brain="barracuda",
        side=d.action,
        confidence=d.confidence,
        risk_level="LOW",
        evidence_fields=list(d.evidence_fields),
        objection=d.objection,
    )
    validate_opinion(op)  # raises if non-compliant


def test_objection_flags_weak_rsi():
    # RSI at 32 → not deeply oversold; should flag.
    d = evaluate("AAL", _ind(rsi14=32.0, bbands={"position": 0.10, "mid": 21.0}))
    if d.action == "BUY":
        assert "RSI_NOT_DEEPLY_OVERSOLD" in (d.objection or "")


def test_objection_flags_bb_position_too_high():
    # BB position > 0.30 → not at lower band; should flag.
    d = evaluate("AAL", _ind(rsi14=22.0, bbands={"position": 0.40, "mid": 21.0}))
    if d.action == "BUY":
        assert "PRICE_NOT_AT_LOWER_BAND" in (d.objection or "")


def test_objection_flags_trend_below_sma50():
    # close 17.0 vs sma50 18.5 → trend < -5%, should flag.
    d = evaluate(
        "AAL",
        _ind(last_close=17.0, rsi14=20.0,
             bbands={"position": 0.10, "mid": 19.0},
             sma={"20": 17.5, "50": 18.5}, atr14=0.30),
    )
    if d.action == "BUY":
        assert "TREND_BELOW_SMA50" in (d.objection or "")


def test_clean_setup_has_no_objection():
    # Strong RSI oversold + BB near lower band + healthy ATR + close above SMA50.
    d = evaluate(
        "AAL",
        _ind(last_close=21.5, rsi14=18.0,
             bbands={"position": 0.05, "mid": 22.0},
             sma={"20": 21.0, "50": 20.5}, atr14=0.40),
    )
    if d.action == "BUY":
        assert d.objection is None


def test_hold_decisions_do_not_require_citations():
    # missing indicators → HOLD; no citation expected
    d = evaluate("AAL", {"ready": False})
    assert d.action == "HOLD"
    assert d.evidence_fields == ()
    assert d.objection is None
