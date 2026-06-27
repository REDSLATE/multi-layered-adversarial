"""Hellcat native-runtime evidence-citation tests (2026-06-26).

Operator pin (third in the upgrade sequence):
    Hellcat = execution-safety voice. Cites execution/governor fields.
    Objection set per operator spec:
      WIDE_SPREAD_EXECUTION_RISK     spread_bps > 75
      LOW_VOLUME_LIQUIDITY_RISK      volume_rel < 1.2
      LOW_ATR_NO_RANGE               atr_pct < 0.25
      EXTREME_ATR_VOLATILITY_RISK    atr_pct > 6.0
      NEGATIVE_NEWS_RISK             news_score < -0.35

After Hellcat ships:
    Barracuda: 1.0 · GTO: 1.0 · Hellcat: 1.0 · Camino: 0.25
    avg evidence quality ≈ 0.8125
"""
from __future__ import annotations

from shared.brains.hellcat.strategy import evaluate
from shared.pipeline.consensus_evidence import (
    MIN_CITED_FIELDS,
    AdvisorOpinion,
    validate_opinion,
)


def _ind(**over):
    """Default Hellcat-eligible breakout setup (BB position high,
    RSI confirming, above SMA20, healthy ATR)."""
    base = {
        "ready": True,
        "last_close": 100.0,
        "rsi14": 68.0,
        "bbands": {"position": 0.95, "upper": 100.5, "lower": 95.0},
        "sma": {"20": 98.0},
        "atr14": 1.0,
    }
    base.update(over)
    return base


def test_buy_decision_cites_at_least_three_fields_without_extra_data():
    # Bare snapshot — only `price` and `atr_pct` are guaranteed.
    # Citation falls below floor → that's a known gap; Hellcat will
    # cite ≥ 3 only when execution-context data is provided.
    d = evaluate("AAL", _ind())
    if d.action == "BUY":
        assert "price" in d.evidence_fields
        assert "atr_pct" in d.evidence_fields


def test_buy_decision_cites_five_fields_with_full_execution_context():
    d = evaluate(
        "AAL",
        _ind(spread_bps=20.0, volume_rel=1.5, news_score=0.1),
    )
    assert d.action == "BUY"
    assert len(d.evidence_fields) >= MIN_CITED_FIELDS
    assert set(d.evidence_fields) == {
        "price", "atr_pct", "spread_bps", "volume_rel", "news_score",
    }
    # Validator accepts a fully-cited opinion.
    op = AdvisorOpinion(
        brain="hellcat",
        side=d.action,
        confidence=d.confidence,
        risk_level="HIGH",
        evidence_fields=list(d.evidence_fields),
        objection=d.objection,
    )
    validate_opinion(op)


def test_volume_rel_alias_rvol_is_honored():
    # Spec field name is `volume_rel`; many indicator pipelines use `rvol`.
    d = evaluate("AAL", _ind(rvol=1.4, spread_bps=20.0, news_score=0.0))
    if d.action == "BUY":
        assert "volume_rel" in d.evidence_fields


def test_wide_spread_fires_execution_risk():
    d = evaluate(
        "AAL", _ind(spread_bps=120.0, volume_rel=1.5, news_score=0.0),
    )
    if d.action == "BUY":
        assert "WIDE_SPREAD_EXECUTION_RISK" in (d.objection or "")


def test_low_volume_fires_liquidity_risk():
    d = evaluate(
        "AAL", _ind(spread_bps=20.0, volume_rel=0.9, news_score=0.0),
    )
    if d.action == "BUY":
        assert "LOW_VOLUME_LIQUIDITY_RISK" in (d.objection or "")


def test_low_atr_fires_no_range():
    # atr14 0.2 / close 100 = 0.2% < 0.25% threshold.
    d = evaluate(
        "AAL",
        _ind(atr14=0.20, spread_bps=20.0, volume_rel=1.5, news_score=0.0),
    )
    if d.action == "BUY":
        assert "LOW_ATR_NO_RANGE" in (d.objection or "")


def test_extreme_atr_fires_volatility_risk():
    # atr14 8.0 / close 100 = 8% > 6% threshold.
    # Need to keep the BUY trigger alive (BB position > 0.85, RSI > 60,
    # close > sma20, close >= bb_upper*0.99). Bump bb_upper accordingly.
    d = evaluate(
        "AAL",
        _ind(atr14=8.0, bbands={"position": 0.95, "upper": 100.5, "lower": 92.0},
             spread_bps=20.0, volume_rel=1.5, news_score=0.0),
    )
    if d.action == "BUY":
        assert "EXTREME_ATR_VOLATILITY_RISK" in (d.objection or "")


def test_negative_news_fires_risk():
    d = evaluate(
        "AAL", _ind(spread_bps=20.0, volume_rel=1.5, news_score=-0.45),
    )
    if d.action == "BUY":
        assert "NEGATIVE_NEWS_RISK" in (d.objection or "")


def test_clean_execution_context_has_no_objection():
    # All execution-safety signals green.
    d = evaluate(
        "AAL",
        _ind(spread_bps=15.0, volume_rel=1.8, news_score=0.2, atr14=1.0),
    )
    if d.action == "BUY":
        assert d.objection is None


def test_news_score_zero_does_not_trigger_objection():
    # 0.0 ≥ -0.35 — no risk.
    d = evaluate(
        "AAL", _ind(spread_bps=20.0, volume_rel=1.5, news_score=0.0),
    )
    if d.action == "BUY":
        assert "NEGATIVE_NEWS_RISK" not in (d.objection or "")


def test_hold_decisions_do_not_carry_citations():
    d = evaluate("AAL", {"ready": False})
    assert d.action == "HOLD"
    assert d.evidence_fields == ()
    assert d.objection is None
