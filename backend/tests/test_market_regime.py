"""Market-regime classifier — unit tests.

Doctrine pin (2026-06-10, P1): the `market_regime` field on every
brain snapshot was hardcoded to "calm" before this pass. Now it's
derived from the runner's universe scan. These tests pin the
classifier's behavior across the regime taxonomy:
  {calm, bull, bear, chop, volatile, crisis}.
"""
from __future__ import annotations

import os
import sys

BACKEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from shared.market_regime import (  # noqa: E402
    classify_market_regime,
    classify_from_symbol_snapshots,
)


# ── Pure classifier ────────────────────────────────────────────────


def test_crisis_trumps_everything():
    """Extreme vol → crisis even with strong bull tape."""
    assert classify_market_regime(
        mean_trend_score=0.9, mean_volatility=0.80, breadth=0.8,
    ) == "crisis"


def test_volatile_when_vol_elevated_but_not_extreme():
    assert classify_market_regime(
        mean_trend_score=0.0, mean_volatility=0.50, breadth=0.0,
    ) == "volatile"


def test_bull_requires_both_trend_and_breadth_up():
    assert classify_market_regime(
        mean_trend_score=0.5, mean_volatility=0.10, breadth=0.5,
    ) == "bull"


def test_bear_requires_both_trend_and_breadth_down():
    assert classify_market_regime(
        mean_trend_score=-0.5, mean_volatility=0.10, breadth=-0.5,
    ) == "bear"


def test_trend_up_breadth_down_is_calm_not_bull():
    """Rotation / divergence — NOT a committed bull move."""
    assert classify_market_regime(
        mean_trend_score=0.5, mean_volatility=0.10, breadth=-0.4,
    ) == "calm"


def test_chop_tight_band():
    """Both trend and breadth flat → chop. Camaro's chop-detection
    relies on this firing correctly."""
    assert classify_market_regime(
        mean_trend_score=0.05, mean_volatility=0.10, breadth=0.05,
    ) == "chop"


def test_mild_signal_outside_tight_band_is_calm():
    """Just outside the chop band but not directional → calm."""
    assert classify_market_regime(
        mean_trend_score=0.15, mean_volatility=0.10, breadth=0.05,
    ) == "calm"


def test_robust_to_none_inputs():
    """Defensive: a None feeder upstream shouldn't crash regime."""
    assert classify_market_regime(
        mean_trend_score=None, mean_volatility=None, breadth=None,  # type: ignore[arg-type]
    ) == "chop"


# ── Snapshot-driven convenience wrapper ──────────────────────────


def _snap(trend, vol, pc):
    return {"trend_score": trend, "volatility": vol, "price_change_pct": pc}


def test_classify_from_snapshots_bull_market():
    snaps = [
        _snap(0.6, 0.15, 1.2),
        _snap(0.4, 0.10, 0.8),
        _snap(0.5, 0.18, 0.5),
        _snap(0.3, 0.12, 0.3),
    ]
    sig = classify_from_symbol_snapshots(snaps)
    assert sig.regime == "bull"
    assert sig.sample_size == 4
    assert sig.breadth == 1.0  # all 4 advanced


def test_classify_from_snapshots_bear_market():
    snaps = [
        _snap(-0.6, 0.15, -1.2),
        _snap(-0.4, 0.10, -0.8),
        _snap(-0.5, 0.18, -0.5),
        _snap(-0.3, 0.12, -0.3),
    ]
    sig = classify_from_symbol_snapshots(snaps)
    assert sig.regime == "bear"
    assert sig.breadth == -1.0


def test_classify_from_snapshots_chop():
    """Mixed advance/decline + small trend ≈ chop."""
    snaps = [
        _snap(0.05, 0.10, 0.05),
        _snap(-0.05, 0.10, -0.05),
        _snap(0.02, 0.10, 0.02),
        _snap(-0.02, 0.10, -0.02),
    ]
    sig = classify_from_symbol_snapshots(snaps)
    assert sig.regime == "chop"


def test_classify_from_snapshots_volatile():
    snaps = [_snap(0.0, 0.50, 0.1)] * 4
    sig = classify_from_symbol_snapshots(snaps)
    assert sig.regime == "volatile"


def test_classify_from_snapshots_crisis():
    snaps = [_snap(0.6, 0.80, 1.2)] * 4
    sig = classify_from_symbol_snapshots(snaps)
    assert sig.regime == "crisis"


def test_classify_from_snapshots_empty():
    """Empty universe → safe default of chop (both trend and breadth
    are zero by definition — falls into the tight band)."""
    sig = classify_from_symbol_snapshots([])
    assert sig.regime == "chop"
    assert sig.sample_size == 0


def test_classify_from_snapshots_missing_fields_dont_crash():
    """A snapshot missing one of the inputs (None/garbage) must not
    crash the classifier — defaults to 0 for that contribution."""
    snaps = [
        {"trend_score": 0.5, "volatility": None, "price_change_pct": "garbage"},
        {"trend_score": 0.4, "volatility": 0.10, "price_change_pct": 0.5},
    ]
    sig = classify_from_symbol_snapshots(snaps)
    # Should not raise, and breadth should reflect just the one
    # numerically-valid advance.
    assert sig.regime in {"calm", "bull", "chop"}
