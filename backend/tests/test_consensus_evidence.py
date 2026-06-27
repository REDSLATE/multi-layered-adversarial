"""Tests for the market-evidence citation doctrine (operator spec, 2026-06-26).

Operator pin:
    A brain may not agree or disagree unless it cites market fields.
    Consensus without cited market evidence does not boost confidence.
    Dissent with cited market evidence overrides shallow agreement.

Tests cover:
  - validate_opinion accepts cited opinions, rejects bare ones
  - validate_opinion rejects unknown field names
  - is_evidence_backed / has_objection / advisor_weight
  - barracuda_advisor reference (data-driven objections)
  - apply_dissent scales boost by 0.25× when advisors are rubber-stampers
  - apply_dissent uses full weight when advisors cite evidence
"""
from __future__ import annotations

import pytest

from shared.pipeline.consensus_dissent import apply_dissent
from shared.pipeline.consensus_evidence import (
    MIN_CITED_FIELDS,
    WEIGHT_FULL,
    WEIGHT_NO_EVIDENCE,
    AdvisorOpinion,
    MarketSnapshot,
    advisor_weight,
    barracuda_advisor,
    has_objection,
    is_evidence_backed,
    validate_opinion,
)


# ── validate_opinion ─────────────────────────────────────────────────


def test_validate_opinion_accepts_three_valid_fields():
    o = AdvisorOpinion(
        brain="hellcat", side="BUY", confidence=0.65, risk_level="NORMAL",
        evidence_fields=["rsi", "vwap", "atr_pct"],
    )
    validate_opinion(o)  # should not raise


def test_validate_opinion_rejects_too_few_fields():
    o = AdvisorOpinion(
        brain="hellcat", side="BUY", confidence=0.65, risk_level="NORMAL",
        evidence_fields=["rsi", "vwap"],
    )
    with pytest.raises(ValueError, match="missing_market_evidence"):
        validate_opinion(o)


def test_validate_opinion_rejects_unknown_field():
    o = AdvisorOpinion(
        brain="hellcat", side="BUY", confidence=0.65, risk_level="NORMAL",
        evidence_fields=["rsi", "vwap", "moonphase"],
    )
    with pytest.raises(ValueError, match="invalid_market_field"):
        validate_opinion(o)


def test_min_cited_fields_is_three():
    assert MIN_CITED_FIELDS == 3


# ── is_evidence_backed / has_objection / advisor_weight ──────────────


def test_evidence_backed_for_dict_with_three_valid_fields():
    assert is_evidence_backed({
        "evidence_fields": ["rsi", "vwap", "spread_bps"],
    }) is True


def test_evidence_backed_false_for_bare_opinion():
    assert is_evidence_backed({"action": "BUY"}) is False


def test_evidence_backed_false_for_unknown_field():
    assert is_evidence_backed({
        "evidence_fields": ["rsi", "vwap", "tarot_reading"],
    }) is False


def test_has_objection_true_for_non_empty_string():
    assert has_objection({"objection": "RSI_OVERBOUGHT"}) is True


def test_has_objection_false_for_empty_or_missing():
    assert has_objection({}) is False
    assert has_objection({"objection": ""}) is False
    assert has_objection({"objection": "   "}) is False


def test_advisor_weight_full_when_evidence_backed():
    assert advisor_weight({
        "evidence_fields": ["rsi", "vwap", "spread_bps"],
    }) == WEIGHT_FULL


def test_advisor_weight_full_when_has_objection_even_without_fields():
    # Dissent with cited objection overrides shallow agreement.
    assert advisor_weight({
        "objection": "PRICE_BELOW_VWAP",
    }) == WEIGHT_FULL


def test_advisor_weight_quarter_when_rubber_stamp():
    assert advisor_weight({"action": "BUY"}) == WEIGHT_NO_EVIDENCE


# ── barracuda_advisor reference (the operator's example) ─────────────


def _snap(**over):
    base = dict(
        symbol="AAL", price=20.0, vwap=19.5, rsi=50.0, atr_pct=2.0,
        volume_rel=1.5, spread_bps=20.0, trend_5m=0.01, trend_1h=0.02,
        news_score=None,
    )
    base.update(over)
    return MarketSnapshot(**base)


def test_barracuda_holds_on_overbought_buy():
    snap = _snap(rsi=75.0)
    op = barracuda_advisor(snap, "BUY")
    assert op.side == "HOLD"
    assert "RSI_OVERBOUGHT_AGAINST_BUY" in (op.objection or "")
    validate_opinion(op)


def test_barracuda_flags_price_below_vwap_on_buy():
    snap = _snap(price=19.0, vwap=19.5, rsi=50.0, volume_rel=1.5,
                 spread_bps=30.0)
    op = barracuda_advisor(snap, "BUY")
    assert "PRICE_BELOW_VWAP_AGAINST_BUY" in (op.objection or "")


def test_barracuda_agrees_when_no_objections():
    snap = _snap(rsi=55.0, price=20.5, vwap=19.5, spread_bps=30.0,
                 volume_rel=1.8)
    op = barracuda_advisor(snap, "BUY")
    assert op.side == "BUY"
    assert op.objection is None
    validate_opinion(op)


def test_barracuda_flags_wide_spread():
    snap = _snap(spread_bps=120.0, rsi=55.0, price=20.5)
    op = barracuda_advisor(snap, "BUY")
    assert "SPREAD_TOO_WIDE" in (op.objection or "")


def test_barracuda_flags_weak_volume():
    snap = _snap(volume_rel=0.8, rsi=55.0, price=20.5, spread_bps=30.0)
    op = barracuda_advisor(snap, "BUY")
    assert "WEAK_RELATIVE_VOLUME" in (op.objection or "")


# ── apply_dissent honors evidence weights ────────────────────────────


def _adv_cited(brain, action, conf):
    return {
        "brain_id": brain, "action": action, "confidence": conf,
        "evidence_fields": ["rsi", "vwap", "spread_bps"],
    }


def _adv_bare(brain, action, conf):
    return {"brain_id": brain, "action": action, "confidence": conf}


def test_apply_dissent_rubber_stamp_pool_yields_quarter_boost():
    # Bare opinions only (no evidence) → boost scaled by 0.25.
    v_bare = apply_dissent(
        executor_action="BUY",
        executor_confidence=0.70,
        advisors=[
            _adv_bare("barracuda", "BUY", 0.68),
            _adv_bare("hellcat", "BUY", 0.66),
            _adv_bare("gto", "BUY", 0.65),
        ],
        raw_boost=0.04,
    )
    v_cited = apply_dissent(
        executor_action="BUY",
        executor_confidence=0.70,
        advisors=[
            _adv_cited("barracuda", "BUY", 0.68),
            _adv_cited("hellcat", "BUY", 0.66),
            _adv_cited("gto", "BUY", 0.65),
        ],
        raw_boost=0.04,
    )
    # Cited pool keeps full boost; bare pool gets ~quarter.
    assert v_cited.boost > v_bare.boost
    assert v_cited.evidence_quality == 1.0
    assert v_bare.evidence_quality == WEIGHT_NO_EVIDENCE


def test_dissent_with_objection_keeps_full_weight():
    # Even without `evidence_fields`, an objection grants full weight.
    v = apply_dissent(
        executor_action="BUY",
        executor_confidence=0.70,
        advisors=[
            {"brain_id": "barracuda", "action": "BUY", "confidence": 0.68,
             "objection": "RSI_OVERBOUGHT"},
            _adv_cited("hellcat", "BUY", 0.66),
            _adv_cited("gto", "BUY", 0.65),
        ],
        raw_boost=0.04,
    )
    assert v.evidence_quality == 1.0


def test_mixed_pool_blends_evidence_quality():
    # One rubber-stamper + two cited → average of weights.
    v = apply_dissent(
        executor_action="BUY",
        executor_confidence=0.70,
        advisors=[
            _adv_cited("barracuda", "BUY", 0.68),
            _adv_cited("hellcat", "BUY", 0.66),
            _adv_bare("gto", "BUY", 0.65),
        ],
        raw_boost=0.04,
    )
    # Expected weights: [1.0, 1.0, 0.25] → avg ≈ 0.75
    assert v.evidence_quality == pytest.approx((1.0 + 1.0 + 0.25) / 3.0, abs=1e-3)
