"""Tripwire tests for the `large_cap_equity_v1` doctrine variant.

Doctrine pin (2026-02-18):
    Mega-caps like AMZN/GOOGL/NVDA must NOT be judged by the small-
    account doctrine's tight ≥10% gap / ≥5x RVOL / ≤20M float
    thresholds — those names will always score REJECT and pollute
    the advisory chips with false signals.

    The large-cap doctrine relaxes thresholds while keeping the SAME
    role-keyed seat shape so audit / scorecard / auto-retire all
    work unchanged. Doctrine (c) is still enforced: governor never
    hard-blocks, risk_multiplier floors at 0.10.
"""
from __future__ import annotations

import pytest

from shared.doctrine.lane_doctrine_router import build_lane_doctrine_packet
from shared.doctrine.large_cap_doctrine import (
    build_large_cap_doctrine_packet,
    DOCTRINE_VERSION,
)


def _large_cap_clean(**overrides):
    base = {
        "lane": "equity",
        "symbol": "NVDA",
        "price": 850.0,
        "gap_pct": 1.5,
        "relative_volume": 2.0,
        "has_news": False,
        "market_regime": "strong",
        "spread_bps": 8,
        "strategy": "large_cap",
        "market_cap_band": "mega",
    }
    base.update(overrides)
    return base


# ─── dispatch ────────────────────────────────────────────────────────


@pytest.mark.tripwire
def test_router_dispatches_to_large_cap_when_strategy_flag():
    packet = build_lane_doctrine_packet(_large_cap_clean(), seat_holders=None)
    assert packet["doctrine_version"] == DOCTRINE_VERSION
    assert packet["lane"] == "equity"


@pytest.mark.tripwire
def test_router_dispatches_to_large_cap_when_market_cap_band_large():
    snap = _large_cap_clean(strategy=None, market_cap_band="large")
    packet = build_lane_doctrine_packet(snap, seat_holders=None)
    assert packet["doctrine_version"] == DOCTRINE_VERSION


@pytest.mark.tripwire
def test_router_dispatches_to_large_cap_when_market_cap_band_mega():
    snap = _large_cap_clean(strategy=None, market_cap_band="mega")
    packet = build_lane_doctrine_packet(snap, seat_holders=None)
    assert packet["doctrine_version"] == DOCTRINE_VERSION


@pytest.mark.tripwire
def test_router_falls_back_to_small_account_when_no_large_cap_flags():
    snap = _large_cap_clean(strategy=None, market_cap_band=None)
    packet = build_lane_doctrine_packet(snap, seat_holders=None)
    # Without large-cap flags we MUST NOT promote the snapshot into
    # large-cap doctrine — falls back to small-account.
    assert packet["doctrine_version"] == "small_account_sidecar_v1"


# ─── shape parity ────────────────────────────────────────────────────


@pytest.mark.tripwire
def test_large_cap_packet_uses_role_keyed_seat_shape():
    packet = build_large_cap_doctrine_packet(_large_cap_clean(),
                                             seat_holders=None)
    assert packet["event_type"] == "BRAIN_DOCTRINE_SIDECAR_PACKET"
    assert set(packet["seats"].keys()) == {
        "strategist", "adversary", "governor", "execution_judge",
    }
    for role in ("strategist", "adversary", "governor", "execution_judge"):
        seat = packet["seats"][role]
        assert seat["role"] == role
        assert seat["may_execute"] is False
        assert "seat" in seat
        assert "holder" in seat


# ─── threshold behavior ──────────────────────────────────────────────


@pytest.mark.tripwire
def test_large_cap_treats_small_gap_as_acceptable():
    """A 2% gap on NVDA should NOT score REJECT under large-cap
    doctrine (it would under small-account, which requires ≥10%)."""
    packet = build_large_cap_doctrine_packet(
        _large_cap_clean(gap_pct=2.0, relative_volume=2.0,
                         market_regime="strong", spread_bps=8),
        seat_holders=None,
    )
    quality = packet["base_labels"]["quality"]
    assert quality != "REJECT", (
        f"large-cap with 2% gap, 2x RVOL, green tape should not be REJECT; "
        f"got quality={quality}, score={packet['base_labels']['score']}"
    )


@pytest.mark.tripwire
def test_large_cap_treats_modest_rvol_as_acceptable():
    """1.8x RVOL on large-cap should label ELEVATED_RELATIVE_VOLUME."""
    packet = build_large_cap_doctrine_packet(
        _large_cap_clean(relative_volume=1.8), seat_holders=None,
    )
    labels = set(packet["base_labels"]["labels"])
    assert "ELEVATED_RELATIVE_VOLUME" in labels


# ─── doctrine (c) enforcement ───────────────────────────────────────


@pytest.mark.tripwire
def test_large_cap_governor_never_hard_blocks_on_reject_quality():
    """Doctrine (c): governor.governor_action must be 'modulate', never
    'block'. risk_multiplier floors at 0.10 — never zero."""
    # Force a REJECT scenario: tiny gap, no rvol, wide spread, weak
    # regime.
    packet = build_large_cap_doctrine_packet(
        _large_cap_clean(
            gap_pct=0.1, relative_volume=0.5,
            market_regime="weak", spread_bps=80,
        ),
        seat_holders=None,
    )
    quality = packet["base_labels"]["quality"]
    gov = packet["seats"]["governor"]
    assert quality == "REJECT"
    assert gov["governor_action"] == "modulate"
    assert gov["risk_multiplier"] >= 0.10  # floor
    assert gov["risk_multiplier"] < 1.0
    # `block_reasons` is informational only under doctrine (c). Its
    # presence does NOT imply the governor hard-blocked.
    assert isinstance(gov.get("block_reasons"), list)


@pytest.mark.tripwire
def test_large_cap_governor_dampens_on_consecutive_losses():
    """consecutive_losses ≥3 should dampen but never zero."""
    packet = build_large_cap_doctrine_packet(
        _large_cap_clean(consecutive_losses=4), seat_holders=None,
    )
    gov = packet["seats"]["governor"]
    assert gov["risk_multiplier"] >= 0.10  # never zero
    assert gov["risk_multiplier"] < 1.0    # but dampened


@pytest.mark.tripwire
def test_large_cap_governor_dampens_on_daily_loss_floor():
    packet = build_large_cap_doctrine_packet(
        _large_cap_clean(daily_pnl=-150), seat_holders=None,
    )
    gov = packet["seats"]["governor"]
    assert gov["risk_multiplier"] >= 0.10
    assert gov["risk_multiplier"] < 1.0


# ─── execution_judge invariants ──────────────────────────────────────


@pytest.mark.tripwire
def test_large_cap_execution_judge_cannot_create_direction():
    """The judge seat is REVIEW-ONLY — it requires an existing trade
    intent and cannot create direction."""
    packet = build_large_cap_doctrine_packet(_large_cap_clean(),
                                             seat_holders=None)
    ej = packet["seats"]["execution_judge"]
    assert ej["may_create_direction"] is False
    assert ej["requires_existing_trade_intent"] is True
    assert ej["may_execute"] is False
