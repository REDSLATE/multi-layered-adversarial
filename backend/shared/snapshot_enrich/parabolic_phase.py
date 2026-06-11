"""Parabolic phase classifier — teaches the brains to read swings.

Operator directive (2026-06-11):
    Stocks like PAVS in the Warrior Trading example (Day 3, volatile &
    hot market) move through 4 distinct phases inside a single
    trading session:

      1. ACCUMULATION — Early run, healthy volume expansion, modest
         distance from VWAP. The doctrine's "GAPPER + HIGH_RVOL"
         setup. Full size, normal stop.

      2. PARABOLIC — Big bars stacking, RVOL accelerating, price now
         well above VWAP. Late-entry risk inverts. Half size,
         tighter stop.

      3. TOPPING — Two consecutive red M1 bars after a green run.
         Distribution starts. Zero new longs. Existing longs should
         exit (handled by the brain's regime-aware logic — when
         market_regime flips to 'weak', brains drop BUY confidence
         and raise SELL).

      4. FADE — Lower lows + lower highs after the peak. RVOL
         collapsing. Don't try to short the bounce.

This module is the classifier ONLY. It produces a phase label + the
underlying velocity / VWAP-distance / RVOL-acceleration measurements
the doctrine and operator UI can read.

Thresholds are env-tunable so the operator can graduate from the
8% parabolic threshold (current) to the 20% threshold (stable run
mode) without code changes.

Fail-soft: needs ≥10 bars for a confident call. Below that, returns
`("unknown", {...measurements...})` and the snapshot's existing
regime classifier stays in charge.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Tuple


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, default))
    except (TypeError, ValueError):
        return default


# Env-tunable thresholds. Operator can graduate to stable-run mode by
# setting these to PARABOLIC_5M=20.0 etc. — no code change needed.
PARABOLIC_5M_THRESHOLD_PCT = _env_float("PARABOLIC_5M_THRESHOLD_PCT", 8.0)
PARABOLIC_VWAP_DIST_PCT = _env_float("PARABOLIC_VWAP_DIST_PCT", 5.0)
PARABOLIC_RVOL_ACCEL = _env_float("PARABOLIC_RVOL_ACCEL", 2.0)
TOPPING_RED_BAR_COUNT = int(_env_float("TOPPING_RED_BAR_COUNT", 2))
FADE_DROP_FROM_PEAK_PCT = _env_float("FADE_DROP_FROM_PEAK_PCT", 3.0)


def _bar_close(b: Dict[str, Any]) -> float:
    return float(b.get("close") or b.get("c") or 0.0)


def _bar_open(b: Dict[str, Any]) -> float:
    return float(b.get("open") or b.get("o") or 0.0)


def _bar_high(b: Dict[str, Any]) -> float:
    return float(b.get("high") or b.get("h") or 0.0)


def _bar_low(b: Dict[str, Any]) -> float:
    return float(b.get("low") or b.get("l") or 0.0)


def _bar_volume(b: Dict[str, Any]) -> float:
    return float(b.get("volume") or b.get("v") or 0.0)


def _is_red(b: Dict[str, Any]) -> bool:
    return _bar_close(b) < _bar_open(b)


def _is_green(b: Dict[str, Any]) -> bool:
    return _bar_close(b) > _bar_open(b)


def _vwap(bars: List[Dict[str, Any]]) -> float:
    """Volume-weighted average across the bar set."""
    num = 0.0
    den = 0.0
    for b in bars:
        typical = (_bar_high(b) + _bar_low(b) + _bar_close(b)) / 3.0
        vol = _bar_volume(b)
        num += typical * vol
        den += vol
    return (num / den) if den > 0 else 0.0


def _pct_change(prior: float, current: float) -> float:
    if prior <= 0:
        return 0.0
    return (current - prior) / prior * 100.0


def classify_parabolic_phase(
    bars: List[Dict[str, Any]],
    current_price: Optional[float] = None,
) -> Tuple[str, Dict[str, float]]:
    """Return `(phase, measurements)` for the given M1 bar history.

    `phase` is one of: ``"accumulation" | "parabolic" | "topping"
    | "fade" | "neutral" | "unknown"``.

    ``measurements`` always contains:
      - ``velocity_1m`` — % change last 1 bar (close vs prior close)
      - ``velocity_5m`` — % change last 5 bars (close vs 5-bars-ago close)
      - ``vwap_distance_pct`` — current vs session VWAP
      - ``rvol_acceleration`` — last-3-bars avg vol / last-30-bars avg vol
      - ``peak_drop_pct`` — % drop from session peak (positive if below)

    Pure / sync. No I/O.
    """
    measurements: Dict[str, float] = {
        "velocity_1m": 0.0,
        "velocity_5m": 0.0,
        "vwap_distance_pct": 0.0,
        "rvol_acceleration": 1.0,
        "peak_drop_pct": 0.0,
    }
    if not bars or len(bars) < 10:
        return "unknown", measurements

    closes = [_bar_close(b) for b in bars]
    if not all(c > 0 for c in closes):
        return "unknown", measurements

    last = float(current_price) if current_price and current_price > 0 else closes[-1]
    measurements["velocity_1m"] = _pct_change(closes[-2], last)
    measurements["velocity_5m"] = _pct_change(closes[-6], last)

    vwap_val = _vwap(bars)
    measurements["vwap_distance_pct"] = _pct_change(vwap_val, last)

    # RVOL acceleration: avg of last 3 bars vs avg of last 30 (or available)
    vols = [_bar_volume(b) for b in bars]
    recent_avg = sum(vols[-3:]) / 3.0 if len(vols) >= 3 else 0.0
    baseline_window = vols[-30:-3] if len(vols) >= 30 else vols[:-3]
    baseline_avg = (sum(baseline_window) / len(baseline_window)) if baseline_window else 0.0
    if baseline_avg > 0:
        measurements["rvol_acceleration"] = round(recent_avg / baseline_avg, 3)

    peak = max(_bar_high(b) for b in bars)
    if peak > 0 and last < peak:
        measurements["peak_drop_pct"] = round(_pct_change(peak, last) * -1.0, 3)

    # Round velocities for cleanliness
    measurements["velocity_1m"] = round(measurements["velocity_1m"], 3)
    measurements["velocity_5m"] = round(measurements["velocity_5m"], 3)
    measurements["vwap_distance_pct"] = round(measurements["vwap_distance_pct"], 3)

    # ── classification (order matters — fade trumps topping trumps parabolic) ──

    # FADE — already broken below recent highs by FADE_DROP_FROM_PEAK_PCT
    if measurements["peak_drop_pct"] >= FADE_DROP_FROM_PEAK_PCT:
        return "fade", measurements

    # TOPPING — last N bars red after a prior green run (≥3 of last 8 green)
    last_n = bars[-TOPPING_RED_BAR_COUNT:]
    prior_8 = bars[-(TOPPING_RED_BAR_COUNT + 8):-TOPPING_RED_BAR_COUNT] if len(bars) >= (TOPPING_RED_BAR_COUNT + 8) else bars[:-TOPPING_RED_BAR_COUNT]
    if (
        len(last_n) == TOPPING_RED_BAR_COUNT
        and all(_is_red(b) for b in last_n)
        and sum(1 for b in prior_8 if _is_green(b)) >= 3
    ):
        return "topping", measurements

    # PARABOLIC — extended run, high velocity, RVOL accelerating
    if (
        measurements["velocity_5m"] >= PARABOLIC_5M_THRESHOLD_PCT
        and measurements["vwap_distance_pct"] >= PARABOLIC_VWAP_DIST_PCT
        and measurements["rvol_acceleration"] >= PARABOLIC_RVOL_ACCEL
    ):
        return "parabolic", measurements

    # ACCUMULATION — early, healthy run (positive velocity, near VWAP)
    if (
        measurements["velocity_5m"] > 1.0
        and abs(measurements["vwap_distance_pct"]) < PARABOLIC_VWAP_DIST_PCT
        and measurements["rvol_acceleration"] >= 1.2
    ):
        return "accumulation", measurements

    return "neutral", measurements


def regime_from_phase(phase: str) -> str:
    """Map parabolic phase → existing `market_regime` enum so the
    doctrine's regime logic in `base_labels.py` picks it up.

    The doctrine recognizes: `strong | green_light | momentum`
    (positive) and `weak | slow | choppy` (negative). Anything else
    is treated as neutral.
    """
    return {
        "accumulation": "green_light",
        "parabolic": "momentum",   # positive but signals "near top" via the new label
        "topping": "weak",         # forces score -= 0.15 in base_labels
        "fade": "weak",
    }.get(phase, "")
