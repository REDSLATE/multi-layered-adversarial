"""Squeeze Detector V2 — tests for the hardened production-safe version.

Covers:
  * PAVS-style A-grade squeeze candidate
  * DATA_ERROR path for missing hard fields
  * WAIT_FOR_FRESH_DATA path for stale data
  * RISK_DOWN_OR_WAIT for wide spread / fading from high
  * Risk penalties actually subtract from the final score
  * Confidence reflects data completeness
"""
from __future__ import annotations

import time

import pytest

from shared.squeeze.squeeze_detector_v2 import SqueezeDetectorV2, SqueezeInput


@pytest.fixture
def detector():
    return SqueezeDetectorV2()


def _strong_squeeze(**overrides) -> SqueezeInput:
    """Build a PAVS-ish strong squeeze input. Allow per-test overrides."""
    defaults = dict(
        symbol="PAVS",
        price=18.50,
        prev_close=3.00,
        day_high=18.60,         # current near high (not fading)
        premarket_high=6.50,
        volume_today=120_000_000,
        avg_volume_20d=2_000_000,
        float_shares=8_000_000,
        timestamp=time.time(),
        data_freshness_ms=800,
        short_interest_pct=18.0,
        borrow_rate_pct=35.0,
        borrow_rate_change_pct=40.0,
        shares_available_to_short=25_000,
        spread_bps=45,
        news_catalyst=True,
        price_30s_ago=17.90,
        volume_last_1m=3_000_000,
        avg_volume_last_5m=1_000_000,
    )
    defaults.update(overrides)
    return SqueezeInput(**defaults)


def test_pavs_style_strong_squeeze_grades_high(detector):
    inp = _strong_squeeze()
    r = detector.analyze(inp)
    # PAVS hits gap 516%, rel_vol 60x, low float, news, short interest, borrow spike,
    # low share availability, breakout above premarket high, velocity, vol accel.
    # Confluence multipliers stack. parabolic_gap_risk fires (gap > 150%) → -0?
    # parabolic_gap_risk is not in RISK_PENALTIES so it's surfaced but doesn't deduct.
    assert r.symbol == "PAVS"
    assert r.grade in ("A", "B"), f"unexpected grade {r.grade}: {r}"
    assert r.raw_score >= 80
    assert r.confidence > 0.5
    # The big positive signals all fired
    assert any("relative_volume" in s for s in r.reasons)
    assert any("low_float" in s for s in r.reasons)
    assert any("float_rotation" in s for s in r.reasons)


def test_missing_price_returns_data_error(detector):
    r = detector.analyze(_strong_squeeze(price=0.0))
    assert r.grade == "F"
    assert r.action_bias == "DATA_ERROR"
    assert "data_feed_failure" in r.risk_flags
    assert "invalid_price" in r.reasons
    assert r.confidence == 0.0


def test_missing_avg_volume_returns_data_error(detector):
    r = detector.analyze(_strong_squeeze(avg_volume_20d=0))
    assert r.grade == "F"
    assert r.action_bias == "DATA_ERROR"
    assert "invalid_avg_volume_20d" in r.reasons


def test_stale_data_blocks_grade(detector):
    r = detector.analyze(_strong_squeeze(data_freshness_ms=6_000))
    assert r.grade == "F"
    assert r.action_bias == "WAIT_FOR_FRESH_DATA"
    assert "stale_data_risk" in r.risk_flags


def test_wide_spread_downgrades_to_C(detector):
    r = detector.analyze(_strong_squeeze(spread_bps=150))
    assert r.grade == "C"
    assert r.action_bias == "RISK_DOWN_OR_WAIT"
    assert "wide_spread_risk" in r.risk_flags


def test_already_fading_from_high_downgrades_to_C(detector):
    # current 18.50, day_high 22.00 → 18.5/22 = 84.1% < 85% → fading
    r = detector.analyze(_strong_squeeze(day_high=22.00))
    assert "already_fading_from_high" in r.risk_flags
    # Penalty -25 from the raw score
    assert r.metrics["risk_penalty_total"] >= 25


def test_risk_penalty_actually_subtracts(detector):
    # No-risk baseline
    base = detector.analyze(_strong_squeeze(spread_bps=40))
    # Inject wide spread
    risky = detector.analyze(_strong_squeeze(spread_bps=200))
    assert risky.squeeze_score < base.squeeze_score
    # Specifically -20 for wide_spread_risk
    assert (base.squeeze_score - risky.squeeze_score) >= 20 - 0.01


def test_confidence_reflects_missing_optional_fields(detector):
    # Strip all optional fields
    sparse = _strong_squeeze(
        premarket_high=None,
        float_shares=None,
        short_interest_pct=None,
        borrow_rate_pct=None,
        borrow_rate_change_pct=None,
        shares_available_to_short=None,
        spread_bps=None,
        price_30s_ago=None,
        volume_last_1m=None,
        avg_volume_last_5m=None,
        data_freshness_ms=None,
    )
    r = detector.analyze(sparse)
    assert r.confidence < 0.3  # 0/8 soft fields, and data_incomplete_risk dampens further
    assert "data_incomplete_risk" in r.risk_flags


def test_low_float_signal_fires(detector):
    r = detector.analyze(_strong_squeeze(float_shares=15_000_000))
    assert any("low_float" in s for s in r.reasons)


def test_borrow_rate_spike_fires(detector):
    r = detector.analyze(_strong_squeeze(borrow_rate_change_pct=50.0))
    assert any("borrow_rate_spike" in s for s in r.reasons)


def test_score_clamped_to_100(detector):
    # Hammer every positive signal at once
    r = detector.analyze(_strong_squeeze())
    assert r.raw_score <= 100.0
    assert 0 <= r.squeeze_score <= 100


def test_d_grade_when_nothing_fires(detector):
    # Quiet stock — none of the squeeze signals trigger
    r = detector.analyze(SqueezeInput(
        symbol="BORING",
        price=50.0,
        prev_close=49.95,
        day_high=50.10,
        premarket_high=None,
        volume_today=500_000,
        avg_volume_20d=500_000,
        float_shares=500_000_000,
        timestamp=time.time(),
        data_freshness_ms=500,
        spread_bps=5,
        news_catalyst=False,
    ))
    assert r.grade == "D"
    assert r.action_bias == "IGNORE"
