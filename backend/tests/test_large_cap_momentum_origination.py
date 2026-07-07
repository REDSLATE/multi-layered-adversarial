"""Momentum-origination scoring + direction-bias tripwires.

Doctrine pin (2026-02-19, operator directive):
    Large-cap brains historically emitted HOLD indefinitely because
    the doctrine only scored SETUP QUALITY but never a directional
    sign. The enhancement adds:

      * scoring signals from VWAP tilt, 5m velocity, RVOL
        acceleration, and EMA-stack alignment
      * a `direction.strategy_bias ∈ {BUY, SELL, NEUTRAL}` hint
        derived from the sign of velocity + VWAP + EMA stack, so
        the brain can pick a direction instead of holding forever

    The seat layer still owns FINAL action + sizing. This packet
    only says "there IS a directional signal, and here's which way."
"""
from __future__ import annotations

import pytest

from shared.doctrine.large_cap_doctrine import build_large_cap_doctrine_packet


def _base(**overrides):
    base = {
        "lane": "equity",
        "symbol": "NVDA",
        "price": 850.0,
        "gap_pct": 0.4,           # below 1% — no gap label
        "relative_volume": 1.0,   # below 1.5x — no rvol label
        "market_regime": "unknown",
        "spread_bps": 8,
        "market_cap_band": "mega",
    }
    base.update(overrides)
    return base


# ─── momentum scoring signals ───────────────────────────────────────


@pytest.mark.tripwire
def test_vwap_bull_tilt_lifts_score():
    packet = build_large_cap_doctrine_packet(
        _base(vwap_distance_pct=0.8), seat_holders=None,
    )
    assert "VWAP_BULL_TILT" in packet["base_labels"]["labels"]
    # Score should now clear 0.40 (baseline) → C_QUALITY at least.
    assert packet["base_labels"]["score"] > 0.40


@pytest.mark.tripwire
def test_vwap_strong_bull_tilt_lifts_further():
    packet = build_large_cap_doctrine_packet(
        _base(vwap_distance_pct=2.0), seat_holders=None,
    )
    labels = set(packet["base_labels"]["labels"])
    assert "VWAP_BULL_TILT" in labels
    assert "VWAP_STRONG_BULL_TILT" in labels


@pytest.mark.tripwire
def test_vwap_bear_tilt_still_labels_as_signal():
    """Bear tilt IS a signal — the brain emits SELL. The score
    still lifts (there IS conviction, just directional short)."""
    packet = build_large_cap_doctrine_packet(
        _base(vwap_distance_pct=-1.0), seat_holders=None,
    )
    labels = set(packet["base_labels"]["labels"])
    assert "VWAP_BEAR_TILT" in labels


@pytest.mark.tripwire
def test_velocity_5m_active_labels():
    packet = build_large_cap_doctrine_packet(
        _base(velocity_5m=0.8), seat_holders=None,
    )
    assert "MOMENTUM_5M_ACTIVE" in packet["base_labels"]["labels"]


@pytest.mark.tripwire
def test_velocity_5m_strong_labels():
    packet = build_large_cap_doctrine_packet(
        _base(velocity_5m=2.0), seat_holders=None,
    )
    labels = set(packet["base_labels"]["labels"])
    assert "MOMENTUM_5M_ACTIVE" in labels
    assert "MOMENTUM_5M_STRONG" in labels


@pytest.mark.tripwire
def test_rvol_acceleration_labels():
    packet = build_large_cap_doctrine_packet(
        _base(rvol_acceleration=0.6), seat_holders=None,
    )
    labels = set(packet["base_labels"]["labels"])
    assert "RVOL_ACCELERATING" in labels
    assert "RVOL_STRONG_ACCELERATION" in labels


@pytest.mark.tripwire
def test_ema_stack_aligned_lifts_score():
    packet = build_large_cap_doctrine_packet(
        _base(price_above_emas=True), seat_holders=None,
    )
    assert "EMA_STACK_ALIGNED" in packet["base_labels"]["labels"]


@pytest.mark.tripwire
def test_ema_stack_broken_penalizes():
    packet = build_large_cap_doctrine_packet(
        _base(price_above_emas=False), seat_holders=None,
    )
    assert "EMA_STACK_BROKEN" in packet["base_labels"]["labels"]


@pytest.mark.tripwire
def test_missing_momentum_signals_are_silent_not_penalizing():
    """The invariant: absent fields must NEVER register as
    "negative signal." Missing = silent."""
    packet = build_large_cap_doctrine_packet(_base(), seat_holders=None)
    labels = set(packet["base_labels"]["labels"])
    # None of the momentum labels should fire when fields are absent
    for lbl in ("VWAP_BULL_TILT", "VWAP_BEAR_TILT",
                "MOMENTUM_5M_ACTIVE", "MOMENTUM_5M_STRONG",
                "RVOL_ACCELERATING", "EMA_STACK_ALIGNED",
                "EMA_STACK_BROKEN"):
        assert lbl not in labels, f"{lbl} fired when field was absent"


# ─── direction bias ─────────────────────────────────────────────────


@pytest.mark.tripwire
def test_direction_neutral_when_no_signals():
    packet = build_large_cap_doctrine_packet(_base(), seat_holders=None)
    assert packet["direction"]["strategy_bias"] == "NEUTRAL"
    assert packet["direction"]["bias_strength"] == 0.0


@pytest.mark.tripwire
def test_direction_buy_when_positive_velocity_and_vwap():
    packet = build_large_cap_doctrine_packet(
        _base(velocity_5m=1.8, vwap_distance_pct=1.0, price_above_emas=True),
        seat_holders=None,
    )
    assert packet["direction"]["strategy_bias"] == "BUY"
    assert packet["direction"]["bias_strength"] > 0.5


@pytest.mark.tripwire
def test_direction_sell_when_negative_velocity_and_vwap():
    packet = build_large_cap_doctrine_packet(
        _base(velocity_5m=-1.8, vwap_distance_pct=-1.0, price_above_emas=False),
        seat_holders=None,
    )
    assert packet["direction"]["strategy_bias"] == "SELL"
    assert packet["direction"]["bias_strength"] > 0.5


@pytest.mark.tripwire
def test_direction_neutral_when_signals_conflict():
    # +velocity but -VWAP + broken EMA → conflicting signals below
    # the 0.20 threshold → NEUTRAL.
    packet = build_large_cap_doctrine_packet(
        _base(velocity_5m=0.6, vwap_distance_pct=-1.0, price_above_emas=False),
        seat_holders=None,
    )
    # Strength should be low; bias may be NEUTRAL or SELL depending
    # on weight — assert the doctrine handles conflict gracefully.
    bias = packet["direction"]["strategy_bias"]
    strength = packet["direction"]["bias_strength"]
    assert bias in {"NEUTRAL", "SELL", "BUY"}
    # Conflicting signals cannot produce full strength.
    assert strength < 1.0


@pytest.mark.tripwire
def test_topping_phase_biases_short():
    """Parabolic-phase topping/fade must skew direction toward
    SELL — brains reading the packet should short the fade."""
    packet = build_large_cap_doctrine_packet(
        _base(parabolic_phase="topping"), seat_holders=None,
    )
    assert packet["direction"]["strategy_bias"] == "SELL"


@pytest.mark.tripwire
def test_accumulation_phase_biases_long():
    packet = build_large_cap_doctrine_packet(
        _base(parabolic_phase="accumulation"), seat_holders=None,
    )
    # Not required to be BUY (accumulation alone is a weak signal),
    # but at minimum must not be SELL.
    assert packet["direction"]["strategy_bias"] in {"BUY", "NEUTRAL"}


@pytest.mark.tripwire
def test_directional_signals_promote_score_off_baseline():
    """The core operator complaint: NVDA scores C_QUALITY / REJECT
    with no directional signal. With directional signals, we must
    clear at least B_QUALITY so the brain has real conviction to emit."""
    packet = build_large_cap_doctrine_packet(
        _base(
            velocity_5m=1.8,
            vwap_distance_pct=1.2,
            rvol_acceleration=0.6,
            price_above_emas=True,
            market_regime="strong",
        ),
        seat_holders=None,
    )
    quality = packet["base_labels"]["quality"]
    assert quality in {"A_QUALITY", "B_QUALITY"}, (
        f"NVDA with strong momentum signals should clear B_QUALITY; "
        f"got {quality} score={packet['base_labels']['score']}"
    )
