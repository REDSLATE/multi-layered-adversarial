"""`optional_float` contract tests.

The doctrine says: never bare `float()` on external data. Every
consumer that needs a numeric value MUST call `optional_float`
and pattern-match on `is None`. These tests pin the invariants
that make that safe.
"""
from __future__ import annotations

import math

import pytest

from mc_pulse.feature_builders.coerce import optional_float


@pytest.mark.parametrize("value,expected", [
    (0, 0.0),
    (1, 1.0),
    (-42, -42.0),
    (3.14, 3.14),
    ("1.5", 1.5),
    ("0", 0.0),
    ("-2.5", -2.5),
    (True, 1.0),        # Python bool is int — accept it
    (False, 0.0),
])
def test_returns_float_on_numeric_input(value, expected):
    result = optional_float(value)
    assert result == expected
    assert isinstance(result, float)


@pytest.mark.parametrize("value", [
    None,
    "",
    "hello",
    "1.2.3",
    "NaN",           # 'NaN' PARSES to NaN → we reject → None
    [1, 2],
    {"x": 1},
    object(),
])
def test_returns_none_on_bad_input(value):
    assert optional_float(value) is None


def test_rejects_nan():
    """NaN is finite() = False; must return None so downstream
    math doesn't propagate NaN into brain confidence."""
    assert optional_float(float("nan")) is None


def test_rejects_positive_infinity():
    assert optional_float(float("inf")) is None


def test_rejects_negative_infinity():
    assert optional_float(float("-inf")) is None


def test_never_raises():
    """The whole point — must NEVER raise on any input. If this
    regresses, callers will hit exception cascades again."""
    # A parade of hostile inputs. None must raise.
    hostile = [
        None, "", "  ", "abc", "NaN", "inf", "-inf",
        float("nan"), float("inf"), float("-inf"),
        [1, 2, 3], {"x": 1}, (1, 2), set(),
        object(), type, lambda x: x,
    ]
    for h in hostile:
        assert optional_float(h) is None    # any raise = pytest failure


def test_build_camino_features_survives_none_close():
    """The exact class of failure that took out GTO/Barracuda/Hellcat
    on ETH/USD: a bar with `c=None` slipping into the feature
    builder. Must return a legitimate snapshot (cold branch if
    too many bars corrupted, otherwise clean hot branch)."""
    from mc_pulse.feature_builders.camino import build_camino_features
    # 20 bars, one with c=None. Should still produce a snapshot;
    # the corrupted bar is silently skipped, remaining count = 19
    # → falls into cold branch (< 20 usable). Snapshot is well-formed.
    bars = []
    for i in range(20):
        bars.append({
            "ts": f"2026-07-11T20:{i:02d}:00+00:00",
            "o": 100.0, "h": 100.5, "l": 99.5,
            "c": None if i == 5 else 100.0 + i * 0.1,     # one bad bar
            "v": 1000.0,
        })
    snap, _ = build_camino_features(symbol="ETH/USD", lane="crypto", bars=bars)
    # Must produce SOMETHING — a well-formed dict with the
    # expected keys. Cold branch is fine here (< 20 usable).
    assert snap["symbol"] == "ETH/USD"
    assert "trend_score" in snap
    assert "spread_bps" in snap
    assert snap.get("real_market_data") is False   # cold branch fired


def test_build_camino_features_survives_none_volume():
    """Symmetric guarantee for volume — a None `v` is the
    plausible NoneType source too."""
    from mc_pulse.feature_builders.camino import build_camino_features
    bars = []
    for i in range(20):
        bars.append({
            "ts": f"2026-07-11T20:{i:02d}:00+00:00",
            "o": 100.0, "h": 100.5, "l": 99.5,
            "c": 100.0 + i * 0.1,
            "v": None if i in (3, 7, 11) else 1000.0,     # three bad
        })
    snap, _ = build_camino_features(symbol="ETH/USD", lane="crypto", bars=bars)
    assert "trend_score" in snap
    assert snap.get("real_market_data") is False


def test_build_camino_features_hot_branch_with_all_clean_bars():
    """Regression guard — the None-safe cleaning must not change
    behavior on the healthy path."""
    from mc_pulse.feature_builders.camino import build_camino_features
    bars = [{
        "ts": f"2026-07-11T20:{i:02d}:00+00:00",
        "o": 100.0, "h": 100.5, "l": 99.5,
        "c": 100.0 + i * 0.5,
        "v": 1000.0,
    } for i in range(25)]
    snap, _ = build_camino_features(symbol="ETH/USD", lane="crypto", bars=bars)
    assert snap.get("real_market_data") is True   # hot branch
    assert snap["trend_score"] > 0                # positive drift → +tscore
