"""Tripwires for the base-formation / consolidation / breakout pattern
detector (`shared.patterns.base_breakout`).

Doctrine pin (2026-05-27, operator-confirmed):
    Pattern signals are DESCRIPTIVE evidence only. Never a gate, never
    a hard block, never modifies authority. These tests lock the math
    + the audit-stable schema so brain-side consumers can trust it.

What's pinned:
  * Composite score in [0, 1].
  * `ready` flag set whenever bars are processed (even all-inactive).
  * Three signals expose the canonical fields downstream readers
    (sidecars, Shelly trainer, replay tool) rely on.
  * Threshold defaults match the operator-approved values.
  * Env-tunable: changing PATTERN_* env flips detection behavior
    without a redeploy.
  * Insufficient-data paths return typed reasons (no exceptions).
"""
from __future__ import annotations

import pytest

from shared.patterns.base_breakout import (
    Config, detect_pattern, reload_env,
)


# ──────────────────────── bar builders ────────────────────────


def _bar(ts: str, o: float, h: float, l: float, c: float, v: float) -> dict:
    return {"ts": ts, "o": o, "h": h, "l": l, "c": c, "v": v}


def _flat_bars(n: int, price: float = 100.0, vol: float = 1_000_000.0,
               start_idx: int = 0) -> list[dict]:
    """N bars all at the same price/volume. Useful for warm-up."""
    out = []
    for i in range(n):
        t = f"2026-01-{(i % 28) + 1:02d}T00:00:00+00:00"
        out.append(_bar(t, price, price, price, price, vol))
    return out


def _trending_bars(n: int, start: float, step: float, vol: float = 1_000_000.0) -> list[dict]:
    """N bars climbing by `step` each. ts is a synthetic monotonic
    iso8601-like string keyed off the index."""
    out = []
    p = start
    for i in range(n):
        t = f"2026-{(i // 30) + 1:02d}-{(i % 30) + 1:02d}T00:00:00+00:00"
        out.append(_bar(t, p, p + 0.1, p - 0.1, p, vol))
        p += step
    return out


# ──────────────────────── schema / contract ────────────────────────


@pytest.mark.tripwire
def test_detect_empty_bars_returns_not_ready():
    sig = detect_pattern([])
    assert sig.ready is False
    assert sig.bars_seen == 0
    assert sig.setup_score == 0.0
    assert sig.ma200_uptrend["active"] is False
    assert sig.consolidation["active"] is False
    assert sig.breakout["active"] is False


@pytest.mark.tripwire
def test_signal_schema_keys_pinned():
    """Downstream consumers (sidecars, Shelly trainer, replay tool)
    rely on the key set. Drift = silent contract break."""
    sig = detect_pattern(_flat_bars(10))
    expected_ma200_keys = {
        "active", "slope_per_bar", "bars_evaluated",
        "ma200_now", "ma200_then",
    }
    expected_consol_keys = {
        "active", "floor", "ceiling", "duration_bars",
        "range_pct_of_ma200", "ma_convergence_score",
        "volume_accumulation_score", "reason",
    }
    expected_breakout_keys = {
        "active", "breakout_pct", "volume_surge_multiple",
        "bars_since_breakout", "ceiling_referenced", "reason",
    }
    assert set(sig.ma200_uptrend.keys()) == expected_ma200_keys
    assert set(sig.consolidation.keys()) == expected_consol_keys
    assert set(sig.breakout.keys()) == expected_breakout_keys
    # Composite always in [0, 1].
    assert 0.0 <= sig.setup_score <= 1.0


@pytest.mark.tripwire
def test_default_thresholds_match_operator_spec():
    """Operator-approved defaults pinned 2026-05-27. Bumping requires
    a doctrine event, not a silent edit."""
    assert Config.ma200_uptrend_bars == 30
    assert Config.consolidation_range_max_pct == 0.12
    assert Config.consolidation_min_bars == 20
    assert Config.ma_convergence_max_pct == 0.03
    assert Config.breakout_ceiling_mult == 1.02
    assert Config.breakout_volume_mult == 1.8
    assert Config.breakout_window_bars == 5
    assert Config.small_cap_float_max_millions == 20.0


# ──────────────────────── insufficient-data paths ────────────────────────


@pytest.mark.tripwire
def test_too_few_bars_for_ma200_returns_inactive():
    bars = _flat_bars(50)  # < 200
    sig = detect_pattern(bars)
    assert sig.ready is True
    assert sig.ma200_uptrend["active"] is False
    assert sig.ma200_uptrend["slope_per_bar"] is None
    # Consolidation also can't be confirmed without MA200 warm-up.
    assert sig.consolidation["active"] is False
    assert sig.breakout["active"] is False


@pytest.mark.tripwire
def test_flat_bars_no_uptrend_no_pattern():
    """200+ flat bars: MA200 slope is 0 → uptrend NOT active."""
    bars = _flat_bars(260)
    sig = detect_pattern(bars)
    assert sig.ma200_uptrend["active"] is False
    assert sig.ma200_uptrend["slope_per_bar"] == 0.0
    # No breakout possible without consolidation transitioning out.
    assert sig.breakout["active"] is False


# ──────────────────────── MA200 uptrend ────────────────────────


@pytest.mark.tripwire
def test_steady_uptrend_activates_ma200_signal():
    bars = _trending_bars(260, start=10.0, step=0.05)
    sig = detect_pattern(bars)
    assert sig.ma200_uptrend["active"] is True
    assert sig.ma200_uptrend["slope_per_bar"] > 0.0
    assert sig.ma200_uptrend["bars_evaluated"] == 30


# ──────────────────────── consolidation + breakout integration ────────────


def _setup_pattern_bars() -> list[dict]:
    """Synthesize the textbook pattern from the Reddit chart:
      * 220 bars of climbing trend → builds MA200 uptrend
      * 25 bars of tight consolidation around 12.5–13.5 → consolidation
      * 1 final bar that closes above ceiling * 1.025 on 2.0× volume → breakout
    """
    bars: list[dict] = []
    # Phase 1: climbing trend, vol baseline 800k
    p = 8.0
    for i in range(220):
        bars.append(_bar(
            f"phase1-{i:04d}", p, p + 0.05, p - 0.05, p + 0.04, 800_000.0,
        ))
        p += 0.022
    # End of phase 1 ~ 8.0 + 220*0.022 ≈ 12.84.
    # Phase 2: 25 bars consolidation between 12.5 and 13.4.
    consol_prices = [12.5, 12.7, 12.9, 13.0, 13.1, 13.3, 13.2, 13.0, 12.8,
                     12.6, 12.7, 12.9, 13.1, 13.2, 13.3, 13.1, 13.0, 12.9,
                     12.8, 12.9, 13.0, 13.1, 13.2, 13.3, 13.2]
    for i, cp in enumerate(consol_prices):
        bars.append(_bar(
            f"phase2-{i:02d}", cp, cp + 0.05, cp - 0.05, cp, 850_000.0,
        ))
    # Phase 3: explosive breakout — close > 13.4 * 1.02 = 13.668; volume 2×.
    bars.append(_bar(
        "phase3-00", 13.3, 14.2, 13.3, 14.1, 1_800_000.0,
    ))
    return bars


@pytest.mark.tripwire
def test_textbook_pattern_fires_all_three_signals():
    """The canonical setup must fire all three signals AND yield a
    high composite score (> 0.55)."""
    bars = _setup_pattern_bars()
    sig = detect_pattern(bars, symbol="DEMO", tf="1d")
    assert sig.ready
    assert sig.ma200_uptrend["active"], sig.ma200_uptrend
    assert sig.consolidation["active"], sig.consolidation
    assert sig.breakout["active"], sig.breakout
    # Breakout metadata is populated.
    assert sig.breakout["breakout_pct"] > 0.0
    assert sig.breakout["volume_surge_multiple"] >= 1.8
    assert sig.breakout["bars_since_breakout"] == 0
    # Composite score weights all three.
    assert sig.setup_score > 0.55, f"setup_score={sig.setup_score}"


@pytest.mark.tripwire
def test_breakout_inactive_when_volume_surge_insufficient():
    """Replace the breakout bar with low-volume close above ceiling.
    Pattern must NOT fire — operator-locked guard against
    no-volume false breakouts."""
    bars = _setup_pattern_bars()
    # Replace breakout bar volume with baseline (no surge).
    bars[-1]["v"] = 800_000.0
    sig = detect_pattern(bars)
    assert sig.consolidation["active"] is True
    assert sig.breakout["active"] is False
    assert sig.breakout["reason"] == "no_breakout_in_window"


@pytest.mark.tripwire
def test_breakout_inactive_when_close_below_ceiling_mult():
    bars = _setup_pattern_bars()
    # Close just at ceiling, not 2% above.
    bars[-1]["c"] = 13.4
    sig = detect_pattern(bars)
    assert sig.consolidation["active"] is True
    assert sig.breakout["active"] is False


# ──────────────────────── env-tunable thresholds ────────────────────────


@pytest.mark.tripwire
def test_tightening_consolidation_range_disqualifies_window(monkeypatch):
    """Same bars; tighter range tolerance must collapse detection."""
    bars = _setup_pattern_bars()

    # Baseline: pattern fires.
    sig_base = detect_pattern(bars)
    assert sig_base.consolidation["active"] is True

    # Tighten to 5% (was 12%). The 12.5–13.4 window spans ~7% of MA200,
    # so it should no longer qualify.
    monkeypatch.setenv("PATTERN_CONSOLIDATION_RANGE_MAX_PCT", "0.05")
    reload_env()
    try:
        sig_tight = detect_pattern(bars)
        assert sig_tight.consolidation["active"] is False
    finally:
        monkeypatch.setenv("PATTERN_CONSOLIDATION_RANGE_MAX_PCT", "0.12")
        reload_env()


@pytest.mark.tripwire
def test_breakout_volume_multiplier_env_tunable(monkeypatch):
    bars = _setup_pattern_bars()
    sig_base = detect_pattern(bars)
    assert sig_base.breakout["active"] is True

    # Tighten multiplier to 3.0× (was 1.8×); the 2× surge no longer qualifies.
    monkeypatch.setenv("PATTERN_BREAKOUT_VOLUME_MULT", "3.0")
    reload_env()
    try:
        sig_tight = detect_pattern(bars)
        assert sig_tight.breakout["active"] is False
    finally:
        monkeypatch.setenv("PATTERN_BREAKOUT_VOLUME_MULT", "1.8")
        reload_env()


# ──────────────────────── small-cap qualifier ────────────────────────


@pytest.mark.tripwire
def test_small_cap_qualifier_none_when_float_unknown():
    sig = detect_pattern(_flat_bars(50))
    assert sig.small_cap_qualified is None


@pytest.mark.tripwire
def test_small_cap_qualifier_true_when_float_below_threshold():
    sig = detect_pattern(_flat_bars(50), float_shares_millions=15.0)
    assert sig.small_cap_qualified is True


@pytest.mark.tripwire
def test_small_cap_qualifier_false_when_float_above_threshold():
    sig = detect_pattern(_flat_bars(50), float_shares_millions=500.0)
    assert sig.small_cap_qualified is False


# ──────────────────────── doctrine guard ────────────────────────


@pytest.mark.tripwire
def test_signals_never_carry_execution_authority():
    """Schema must never emit may_execute / authority fields as KEYS.
    Pattern signals are evidence, not authority. (We grep for keys —
    the doctrine_note string is allowed to *mention* authority since
    its job is to remind readers what the packet is NOT.)"""
    sig = detect_pattern(_setup_pattern_bars())
    from dataclasses import asdict
    body = asdict(sig)
    banned_keys = {"may_execute", "execute_now", "authority",
                   "requires_gate", "force_buy"}

    def _walk_keys(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                yield k
                yield from _walk_keys(v)
        elif isinstance(obj, list):
            for item in obj:
                yield from _walk_keys(item)

    leaked = banned_keys & set(_walk_keys(body))
    assert not leaked, (
        f"banned keys leaked into PatternSignals: {leaked} — "
        f"evidence must never carry execution authority"
    )


# ──────────────────────── composite score sanity ────────────────────────


@pytest.mark.tripwire
def test_composite_score_capped_at_one():
    """Even if every input is maxed, composite never exceeds 1.0."""
    bars = _setup_pattern_bars()
    # Inflate breakout characteristics — should still cap at 1.0.
    bars[-1]["c"] = 50.0
    bars[-1]["v"] = 100_000_000.0
    sig = detect_pattern(bars)
    assert 0.0 <= sig.setup_score <= 1.0


@pytest.mark.tripwire
def test_composite_score_zero_on_empty_bars():
    sig = detect_pattern([])
    assert sig.setup_score == 0.0


# ──────────────────────── config snapshot ────────────────────────


@pytest.mark.tripwire
def test_config_snapshot_carried_on_result():
    """Each signals packet carries the live thresholds — required for
    historical replay (Shelly substrate)."""
    sig = detect_pattern(_flat_bars(50))
    cs = sig.config_snapshot
    assert cs["ma200_uptrend_bars"] == 30
    assert cs["consolidation_range_max_pct"] == 0.12
    assert cs["breakout_volume_mult"] == 1.8
    # Pinned: keys MUST stay stable so replay tools can compare across versions.
    expected_keys = {
        "ma200_uptrend_bars", "consolidation_range_max_pct",
        "consolidation_min_bars", "ma_convergence_max_pct",
        "breakout_ceiling_mult", "breakout_volume_mult",
        "breakout_window_bars", "small_cap_float_max_millions",
    }
    assert set(cs.keys()) == expected_keys
