"""Tests for the spread-quality guard (2026-07-03).

Locks the fix for the "stale/sentinel quote → forced HOLD" bug found
in prod on 2026-07-03. The root cause was that three separate scorers
read `spread_bps` without checking `spread_quality`, treating stale
after-hours values (500-9999 bps) as if they were real wide spreads:

    1. `backend/shared/doctrine/base_labels.py` — SPREAD_TOO_WIDE label
    2. `backend/shared/doctrine/large_cap_doctrine.py` — same label
    3. `external/brains/brain_core.py::_build_hypotheses` — HOLD/OBSERVE
        hypothesis scores clamp to 1.0 at high spread_bps values

Fix contract:
    * When snapshot["spread_quality"] ∈ {"stale", "sentinel"}:
        - Doctrine labelers: no SPREAD_TOO_WIDE, no score deduction;
          instead emit informational SPREAD_QUALITY_UNKNOWN label.
        - Hypothesis builder: substitute spread_bps=25.0 (neutral)
          so HOLD/OBSERVE don't pin to 1.0.
    * When spread_quality ∈ {"live", <missing>, <anything else>}:
        - Existing behavior preserved (tight/acceptable/wide ladder).

The 25.0 substitution value is chosen to be:
    * below the doctrine's wide-spread threshold (75/25 bps) so it
      doesn't cascade into SPREAD_TOO_WIDE via the substituted value
    * high enough to not artificially boost BUY/SELL scores either
"""
from __future__ import annotations

import sys

import pytest

sys.path.insert(0, "/app")
sys.path.insert(0, "/app/backend")


# ─── base_labels.py — the generic doctrine labeler ─────────────

def _snap_base(spread_bps: float, spread_quality: str = "live") -> dict:
    """Minimal snapshot that would otherwise pass base_labels."""
    return {
        "symbol": "TEST", "price": 8.0, "gap_pct": 25.0,
        "relative_volume": 8.0, "has_news": True,
        "float_millions": 5.0, "pattern": "gap_and_go",
        "market_regime": "healthy",
        "spread_bps": spread_bps,
        "spread_quality": spread_quality,
        "hour_et": 10,
    }


def test_base_labels_live_wide_spread_still_penalizes():
    """Regression guard — the fix must not disable the wide-spread
    penalty for LIVE quotes."""
    from shared.doctrine.base_labels import build_doctrine_labels
    r = build_doctrine_labels(_snap_base(200.0, "live"))
    assert "SPREAD_TOO_WIDE" in r.labels
    assert "SPREAD_QUALITY_UNKNOWN" not in r.labels
    assert "spread_too_wide" in r.reasons


def test_base_labels_stale_quote_skips_wide_spread():
    """Bug fix: stale quotes must NOT be labeled SPREAD_TOO_WIDE
    even if spread_bps is huge — that's a no-fresh-quote signal."""
    from shared.doctrine.base_labels import build_doctrine_labels
    r = build_doctrine_labels(_snap_base(2150.0, "stale"))
    assert "SPREAD_TOO_WIDE" not in r.labels
    assert "SPREAD_QUALITY_UNKNOWN" in r.labels
    assert "stale_or_sentinel_quote_no_penalty" in r.reasons


def test_base_labels_sentinel_quote_skips_wide_spread():
    from shared.doctrine.base_labels import build_doctrine_labels
    r = build_doctrine_labels(_snap_base(9999.0, "sentinel"))
    assert "SPREAD_TOO_WIDE" not in r.labels
    assert "SPREAD_QUALITY_UNKNOWN" in r.labels


def test_base_labels_missing_quality_defaults_to_live():
    """Backward-compat: snapshots without spread_quality behave as if
    quality=live (existing behavior preserved)."""
    from shared.doctrine.base_labels import build_doctrine_labels
    snap = _snap_base(200.0)
    snap.pop("spread_quality", None)
    r = build_doctrine_labels(snap)
    assert "SPREAD_TOO_WIDE" in r.labels


def test_base_labels_stale_below_threshold_still_no_penalty():
    """Even a stale-quality quote with a *tight* bps value gets the
    quality-unknown label, not the acceptable label — the substitution
    is about quality, not the bps value itself."""
    from shared.doctrine.base_labels import build_doctrine_labels
    r = build_doctrine_labels(_snap_base(20.0, "stale"))
    assert "SPREAD_QUALITY_UNKNOWN" in r.labels
    assert "SPREAD_ACCEPTABLE" not in r.labels


# ─── large_cap_doctrine.py — same guard ───────────────────────

def _snap_large_cap(spread_bps: float, spread_quality: str = "live") -> dict:
    return {
        "symbol": "NVDA", "gap_pct": 2.0, "relative_volume": 2.0,
        "has_news": False, "market_regime": "healthy",
        "spread_bps": spread_bps, "spread_quality": spread_quality,
        "fractional_supported": True,
    }


def test_large_cap_live_wide_spread_still_penalizes():
    from shared.doctrine.large_cap_doctrine import _build_large_cap_labels
    r = _build_large_cap_labels(_snap_large_cap(50.0, "live"))
    assert "SPREAD_TOO_WIDE" in r.labels


def test_large_cap_stale_quote_skips_wide_spread():
    """The NVDA hour_et=22 case from the operator report — spread_bps
    2150 with spread_quality=sentinel must not force SPREAD_TOO_WIDE."""
    from shared.doctrine.large_cap_doctrine import _build_large_cap_labels
    r = _build_large_cap_labels(_snap_large_cap(2150.0, "sentinel"))
    assert "SPREAD_TOO_WIDE" not in r.labels
    assert "SPREAD_QUALITY_UNKNOWN" in r.labels


def test_large_cap_stale_quote_skips_tight_and_acceptable_too():
    """Neither SPREAD_TIGHT nor SPREAD_ACCEPTABLE — the whole ladder
    is short-circuited when quality is unknown."""
    from shared.doctrine.large_cap_doctrine import _build_large_cap_labels
    r = _build_large_cap_labels(_snap_large_cap(5.0, "stale"))
    assert "SPREAD_TIGHT" not in r.labels
    assert "SPREAD_ACCEPTABLE" not in r.labels
    assert "SPREAD_QUALITY_UNKNOWN" in r.labels


# ─── brain_core.py — hypothesis builder ────────────────────────

def _get_hypothesis_scores(hypotheses) -> dict:
    """Turn a list of Hypothesis objects into a name→score dict."""
    return {h.name: h.score for h in hypotheses}


def test_hypotheses_live_high_spread_still_favors_hold():
    """Regression guard: with LIVE quality and genuinely-wide spread,
    HOLD should still score high — the fix must not neuter the real
    market signal."""
    from mc_brains._legacy.brain_core import NeutralAdversarialBrain
    brain = NeutralAdversarialBrain(brain_id="test", display_name="Test")
    hypotheses = brain._build_hypotheses({
        "spread_bps": 500.0, "spread_quality": "live",
        "volatility": 0.6, "liquidity_score": 0.5,
        "trend_score": 0.1, "rsi": 50.0,
        "price_change_pct": 0.0, "volume_change_pct": 0.0,
    })
    scores = _get_hypothesis_scores(hypotheses)
    # 500 bps live * 0.002 = 1.0 clamp on HOLD; still expected.
    assert scores["hypothesis_hold"] >= 0.9


def test_hypotheses_stale_high_spread_does_NOT_pin_hold():
    """THE bug fix — stale quality with spread_bps=2150 must NOT
    force HOLD to 1.0. The substitution to 25 bps is the cure."""
    from mc_brains._legacy.brain_core import NeutralAdversarialBrain
    brain = NeutralAdversarialBrain(brain_id="test", display_name="Test")
    hypotheses = brain._build_hypotheses({
        "spread_bps": 2150.0, "spread_quality": "stale",
        "volatility": 0.66, "liquidity_score": 0.87,
        "trend_score": 0.1, "rsi": 50.0,
        "price_change_pct": 0.0, "volume_change_pct": 0.0,
    })
    scores = _get_hypothesis_scores(hypotheses)
    # HOLD should now be MUCH lower — around 0.45 + 25*0.002 + noise
    # = ~0.50, definitely under 0.75 (the "HOLD dominates" threshold).
    assert scores["hypothesis_hold"] < 0.75, (
        f"stale-quote spread substitution failed: HOLD={scores['hypothesis_hold']}"
    )
    assert scores["hypothesis_observe"] < 0.75


def test_hypotheses_sentinel_quote_neutralized():
    """Same guarantee for `sentinel` quality (the NVDA case)."""
    from mc_brains._legacy.brain_core import NeutralAdversarialBrain
    brain = NeutralAdversarialBrain(brain_id="test", display_name="Test")
    hypotheses = brain._build_hypotheses({
        "spread_bps": 9999.0, "spread_quality": "sentinel",
        "volatility": 0.66, "liquidity_score": 0.87,
        "trend_score": 0.1, "rsi": 50.0,
        "price_change_pct": 0.0, "volume_change_pct": 0.0,
    })
    scores = _get_hypothesis_scores(hypotheses)
    assert scores["hypothesis_hold"] < 0.75
    assert scores["hypothesis_observe"] < 0.75


def test_hypotheses_live_quality_unchanged_backward_compat():
    """Snapshots without spread_quality (existing behavior) treated
    as `live` — must produce the same result as before the fix."""
    from mc_brains._legacy.brain_core import NeutralAdversarialBrain
    brain = NeutralAdversarialBrain(brain_id="test", display_name="Test")
    hypotheses = brain._build_hypotheses({
        "spread_bps": 20.0,   # tight live spread
        "volatility": 0.30, "liquidity_score": 0.90,
        "trend_score": 0.3, "rsi": 55.0,
        "price_change_pct": 0.5, "volume_change_pct": 0.2,
    })
    scores = _get_hypothesis_scores(hypotheses)
    # Live tight spread should NOT force HOLD dominance.
    assert scores["hypothesis_hold"] < 0.75


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
