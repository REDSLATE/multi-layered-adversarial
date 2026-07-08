"""Tests for `velocity_5m` + `market_regime` in `session_features` (Follow-up A).

Doctrine (2026-02-20):
    velocity_5m = second-derivative curvature = (c[-1] - 2*c[-2] + c[-3]) / c[-2]
        Positive = accelerating up; negative = decelerating/rolling over.
        Requires ≥ 3 bars in today's session. Missing → None.

    market_regime = SHARED value (SPY-based, TTL-cached).
        Injected as a parameter to session_features / build_snapshot,
        NOT computed per-symbol.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, "/app/backend")

from shared.indicators import build_snapshot, session_features


def _bar(ts: str, o: float, h: float, l: float, c: float, v: float) -> dict:
    return {"ts": ts, "o": o, "h": h, "l": l, "c": c, "v": v}


def _make_session(closes: list[float], date: str = "2026-07-08") -> list[dict]:
    """Build a today-session bar list with the given close prices,
    at 1-minute spacing. Volumes uniform, price shape driven by closes."""
    bars = []
    for i, c in enumerate(closes):
        ts = f"{date}T14:{30 + i:02d}:00+00:00"
        bars.append(_bar(ts, o=c, h=c * 1.001, l=c * 0.999, c=c, v=1_000_000))
    return bars


# ─────────────────────── velocity_5m ───────────────────────


def test_velocity_5m_accelerating_up():
    """Convex-up shape: closes 100 → 101 → 103. Second diff = +1."""
    bars = _make_session([100.0, 101.0, 103.0])
    result = session_features(bars)
    # v = (103 - 2*101 + 100) / 101 = (103 - 202 + 100) / 101 = 1 / 101 ≈ 0.0099
    assert result["velocity_5m"] is not None
    assert result["velocity_5m"] > 0
    assert abs(result["velocity_5m"] - (1.0 / 101.0)) < 1e-9


def test_velocity_5m_decelerating():
    """Concave-down shape: 100 → 103 → 104. Second diff = -2."""
    bars = _make_session([100.0, 103.0, 104.0])
    result = session_features(bars)
    # v = (104 - 2*103 + 100) / 103 = -2 / 103 ≈ -0.0194
    assert result["velocity_5m"] is not None
    assert result["velocity_5m"] < 0


def test_velocity_5m_steady_trend_is_zero():
    """Linear trend: 100 → 101 → 102. Second diff = 0."""
    bars = _make_session([100.0, 101.0, 102.0])
    result = session_features(bars)
    assert result["velocity_5m"] == 0.0


def test_velocity_5m_none_when_too_few_bars():
    """Fewer than 3 bars → None. Rev of the LOOKBACK contract."""
    bars = _make_session([100.0, 101.0])  # only 2
    result = session_features(bars)
    assert result["velocity_5m"] is None


def test_velocity_5m_uses_last_3_bars_of_session():
    """When session has many bars, velocity uses ONLY the last three."""
    # 20-bar session ending with an accelerating tail: ... 105, 106, 108
    closes = list(range(80, 100)) + [105.0, 106.0, 108.0]
    bars = _make_session([float(c) for c in closes])
    result = session_features(bars)
    # Ignore the linear ramp; last 3 are (105, 106, 108).
    # v = (108 - 2*106 + 105) / 106 = 1 / 106
    assert abs(result["velocity_5m"] - (1.0 / 106.0)) < 1e-9


# ─────────────────────── market_regime injection ───────────────────────


def test_market_regime_defaults_to_none_when_not_supplied():
    bars = _make_session([100.0, 101.0, 102.0])
    result = session_features(bars)
    assert result["market_regime"] is None


def test_market_regime_passes_through_from_arg():
    bars = _make_session([100.0, 101.0, 102.0])
    result = session_features(bars, market_regime="bull")
    assert result["market_regime"] == "bull"

    result = session_features(bars, market_regime="choppy")
    assert result["market_regime"] == "choppy"

    result = session_features(bars, market_regime="bear")
    assert result["market_regime"] == "bear"


def test_market_regime_survives_empty_bars_path():
    """No bars → session_features returns early. The market_regime
    arg must still land on the output — otherwise callers can't
    distinguish 'symbol has no bars yet' from 'regime unknown'."""
    result = session_features([], market_regime="bull")
    assert result["market_regime"] == "bull"


# ─────────────────────── build_snapshot integration ───────────────────────


def test_build_snapshot_threads_market_regime_arg():
    """build_snapshot must accept + thread market_regime into the
    session_features contribution."""
    bars = _make_session([100.0] * 25)  # enough for indicator warm-up
    snap = build_snapshot(bars, market_regime="bull")
    assert snap["market_regime"] == "bull"


def test_build_snapshot_backward_compatible_without_regime():
    """Prior callers (test suites, tools) don't pass market_regime.
    build_snapshot must default it to None, not crash."""
    bars = _make_session([100.0] * 25)
    snap = build_snapshot(bars)
    assert snap["market_regime"] is None
    # Existing session_features_v2 fields still land.
    assert "trend_score" in snap
    assert "velocity_5m" in snap


# ─────────────────────── market_regime resolver classifier ───────────────────────


def test_regime_classifier_bull():
    """Positive trend + low vol → bull."""
    from shared.market_regime import _classify
    assert _classify(
        trend_20d=0.05, realized_vol=0.008,
        trend_threshold=0.02, vol_choppy=0.015,
    ) == "bull"


def test_regime_classifier_bear():
    """Negative trend past threshold → bear. Vol ignored."""
    from shared.market_regime import _classify
    assert _classify(
        trend_20d=-0.03, realized_vol=0.005,
        trend_threshold=0.02, vol_choppy=0.015,
    ) == "bear"
    assert _classify(
        trend_20d=-0.03, realized_vol=0.05,   # high vol
        trend_threshold=0.02, vol_choppy=0.015,
    ) == "bear"


def test_regime_classifier_choppy_flat():
    """Small trend (below threshold) → choppy."""
    from shared.market_regime import _classify
    assert _classify(
        trend_20d=0.005, realized_vol=0.005,
        trend_threshold=0.02, vol_choppy=0.015,
    ) == "choppy"


def test_regime_classifier_choppy_high_vol_uptrend():
    """Trending up but vol above choppy threshold → still choppy."""
    from shared.market_regime import _classify
    assert _classify(
        trend_20d=0.03, realized_vol=0.02,   # vol > choppy_pct
        trend_threshold=0.02, vol_choppy=0.015,
    ) == "choppy"
