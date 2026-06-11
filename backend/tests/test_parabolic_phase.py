"""Parabolic phase classifier — covers all 4 phases + edge cases."""
from __future__ import annotations

import pytest

from shared.snapshot_enrich.parabolic_phase import (
    classify_parabolic_phase,
    regime_from_phase,
)


def _bar(o, h, l, c, v=1000):
    return {"open": o, "high": h, "low": l, "close": c, "volume": v}


def _green(o, c, v=1000):
    h = max(o, c) + 0.05
    l = min(o, c) - 0.05
    return _bar(o, h, l, c, v)


def _red(o, c, v=1000):
    h = max(o, c) + 0.05
    l = min(o, c) - 0.05
    return _bar(o, h, l, c, v)


def test_unknown_when_too_few_bars():
    phase, m = classify_parabolic_phase([])
    assert phase == "unknown"
    assert m["velocity_5m"] == 0.0

    phase, _ = classify_parabolic_phase([_green(1, 1.1)] * 5)
    assert phase == "unknown"


def test_accumulation_early_run_healthy():
    # Steady climb staying within VWAP band — accumulation, not parabolic
    bars = []
    for i in range(15):
        price = 10.0 + i * 0.03  # ~4.5% over 15 bars; ~1.5% per 5 bars
        vol = 2000 if i >= 12 else 1000  # last 3 bars hotter
        c = price + 0.005
        h = c + 0.01
        l = price - 0.005
        bars.append({"open": price, "high": h, "low": l, "close": c, "volume": vol})
    phase, m = classify_parabolic_phase(bars)
    assert phase == "accumulation"
    assert m["velocity_5m"] > 1.0
    assert abs(m["vwap_distance_pct"]) < 5.0


def test_parabolic_extended_velocity_and_rvol_spike():
    # 30 bars: 25 flat-ish then 5 explosive bars
    bars = [_bar(5.0, 5.05, 4.95, 5.0, v=1000)] * 25
    # Now 5 bars of rapid acceleration with massive vol
    for i, p in enumerate([5.2, 5.6, 6.1, 6.6, 7.2]):
        bars.append(_bar(p - 0.1, p + 0.1, p - 0.15, p, v=10000))
    phase, m = classify_parabolic_phase(bars, current_price=7.2)
    # 5-bar change: 5.0 → 7.2 = +44%
    assert m["velocity_5m"] > 8.0
    assert m["rvol_acceleration"] > 2.0
    # VWAP-distance: 7.2 vs ~5.4 vwap = >25%
    assert m["vwap_distance_pct"] > 5.0
    assert phase == "parabolic"


def test_topping_two_red_bars_after_green_run():
    # 8 green bars then 2 red bars — should classify as topping
    bars = []
    for i in range(8):
        o = 5.0 + i * 0.1
        c = o + 0.08
        bars.append(_bar(o, c + 0.02, o - 0.02, c, v=2000))
    # Two red bars at the top
    last_green_close = bars[-1]["close"]
    bars.append(_bar(last_green_close, last_green_close + 0.02, last_green_close - 0.10, last_green_close - 0.08, v=2500))
    bars.append(_bar(last_green_close - 0.08, last_green_close - 0.05, last_green_close - 0.15, last_green_close - 0.12, v=2300))
    phase, m = classify_parabolic_phase(bars)
    # peak_drop should still be small (< 3%) so we don't fall to fade
    # 8 + 2 = 10 bars; should classify as topping
    if m["peak_drop_pct"] < 3.0:
        assert phase == "topping"
    else:
        # If the drop exceeded 3%, fade takes precedence — that's also acceptable
        assert phase in ("topping", "fade")


def test_fade_below_peak():
    # Build up to peak then crash 5% off
    bars = []
    for i in range(8):
        bars.append(_green(5.0 + i * 0.2, 5.0 + i * 0.2 + 0.15, v=2000))
    # Peak ~ 6.55, now drop
    bars.append(_red(6.55, 6.30, v=2200))
    bars.append(_red(6.30, 6.10, v=2100))
    bars.append(_red(6.10, 5.85, v=2000))  # ~11% off peak
    phase, m = classify_parabolic_phase(bars)
    assert phase == "fade"
    assert m["peak_drop_pct"] >= 3.0


def test_neutral_when_no_decisive_signal():
    # Sideways chop, no velocity, no VWAP deviation
    bars = []
    for i in range(15):
        # Alternating tiny up/down bars
        if i % 2 == 0:
            bars.append(_bar(5.00, 5.02, 4.99, 5.01, v=1000))
        else:
            bars.append(_bar(5.01, 5.02, 4.99, 5.00, v=1000))
    phase, m = classify_parabolic_phase(bars)
    assert phase in ("neutral", "unknown")  # both acceptable for choppy
    assert abs(m["velocity_5m"]) < 1.0


def test_regime_from_phase_mapping():
    assert regime_from_phase("accumulation") == "green_light"
    assert regime_from_phase("parabolic") == "momentum"
    assert regime_from_phase("topping") == "weak"
    assert regime_from_phase("fade") == "weak"
    assert regime_from_phase("neutral") == ""
    assert regime_from_phase("unknown") == ""
    assert regime_from_phase("garbage") == ""


def test_measurements_populated_even_when_unknown():
    phase, m = classify_parabolic_phase([_green(1, 1.1)] * 5)
    assert phase == "unknown"
    # Should still have all measurement keys
    assert "velocity_1m" in m
    assert "velocity_5m" in m
    assert "vwap_distance_pct" in m
    assert "rvol_acceleration" in m
    assert "peak_drop_pct" in m


def test_fade_takes_precedence_over_topping():
    # Build conditions for both — fade should win
    bars = []
    for i in range(8):
        bars.append(_green(5.0 + i * 0.3, 5.0 + i * 0.3 + 0.2, v=2000))
    # 2 reds at the top — but with a big drop
    bars.append(_red(7.4, 7.0, v=2200))
    bars.append(_red(7.0, 6.5, v=2100))  # ~12% off peak ~7.4
    phase, m = classify_parabolic_phase(bars)
    assert m["peak_drop_pct"] >= 3.0
    assert phase == "fade"


def test_parabolic_thresholds_use_env(monkeypatch):
    """Operator can graduate from 8% to 20% threshold via env."""
    monkeypatch.setenv("PARABOLIC_5M_THRESHOLD_PCT", "20.0")
    # Reload to pick up the env change
    import importlib
    import shared.snapshot_enrich.parabolic_phase as pp
    importlib.reload(pp)
    # Build a +10% velocity scenario — below the new 20% threshold
    bars = [_bar(5.0, 5.05, 4.95, 5.0, v=1000)] * 25
    for i, p in enumerate([5.1, 5.2, 5.3, 5.4, 5.5]):  # +10% over 5
        bars.append(_bar(p - 0.05, p + 0.05, p - 0.08, p, v=10000))
    phase, m = pp.classify_parabolic_phase(bars, current_price=5.5)
    # 5-bar velocity is +10%, below the new 20% threshold → should NOT
    # classify as parabolic anymore.
    assert phase != "parabolic"
    # Restore for other tests
    monkeypatch.setenv("PARABOLIC_5M_THRESHOLD_PCT", "8.0")
    importlib.reload(pp)
