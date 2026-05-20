"""Tests for the strategy-split doctrine (Phase C, source-aligned).

Verifies that:
  • `strategy=gap_and_go` dispatches to `gap_and_go_v1` doctrine
  • `strategy=micro_pullback` dispatches to `micro_pullback_v1`
  • absent / unknown strategy falls back to `small_account_sidecar_v1`
  • SAC2024 refinement: pullback on non-leader does NOT score
  • Each strategy uses the same role-keyed seat shape so existing
    audit / scorecard / auto-retire layers work unchanged.
"""
from __future__ import annotations

from shared.doctrine.lane_doctrine_router import build_lane_doctrine_packet
from shared.doctrine.strategy_doctrines import build_strategy_packet


def _good_gap_and_go_snapshot(**overrides):
    base = {
        "lane": "equity",
        "symbol": "TEST",
        "price": 7.50,
        "gap_pct": 25,                    # STRONG_GAPPER
        "relative_volume": 8,
        "has_news": True,
        "float_millions": 5,              # ULTRA_LOW_FLOAT
        "pattern": "bull_flag",
        "market_regime": "strong",
        "spread_bps": 40,
        # strategy-specific
        "premarket_high_crossed": True,
        "premarket_bull_flag": True,
        "price_above_emas": True,
    }
    base.update(overrides)
    return base


def _good_micro_pullback_snapshot(**overrides):
    base = {
        "lane": "equity",
        "symbol": "TEST",
        "price": 7.50,
        "gap_pct": 22,
        "relative_volume": 8,
        "has_news": True,
        "float_millions": 10,
        "pattern": "micro_pullback",
        "market_regime": "strong",
        "spread_bps": 40,
        # strategy-specific
        "near_half_or_whole_dollar": True,
        "momentum_active": True,
        "no_nearby_resistance": True,
        "pullback_low": 7.10,
    }
    base.update(overrides)
    return base


# ─── dispatch ────────────────────────────────────────────────────────

def test_router_dispatches_to_gap_and_go_v1():
    snap = _good_gap_and_go_snapshot(strategy="gap_and_go")
    packet = build_lane_doctrine_packet(snap, seat_holders=None)
    assert packet["doctrine_version"] == "gap_and_go_v1"
    assert packet["lane"] == "equity"
    assert set(packet["seats"].keys()) == {
        "strategist", "adversary", "governor", "execution_judge",
    }


def test_router_dispatches_to_micro_pullback_v1():
    snap = _good_micro_pullback_snapshot(strategy="micro_pullback")
    packet = build_lane_doctrine_packet(snap, seat_holders=None)
    assert packet["doctrine_version"] == "micro_pullback_v1"
    assert packet["lane"] == "equity"


def test_router_falls_back_to_small_account_when_strategy_absent():
    snap = _good_gap_and_go_snapshot()  # no strategy field
    packet = build_lane_doctrine_packet(snap, seat_holders=None)
    assert packet["doctrine_version"] == "small_account_sidecar_v1"


def test_router_falls_back_on_unknown_strategy():
    snap = _good_gap_and_go_snapshot(strategy="moon_breakout")
    packet = build_lane_doctrine_packet(snap, seat_holders=None)
    assert packet["doctrine_version"] == "small_account_sidecar_v1"


# ─── gap_and_go behavior ─────────────────────────────────────────────

def test_gap_and_go_clean_setup_is_execution_ready():
    snap = _good_gap_and_go_snapshot(strategy="gap_and_go")
    packet = build_strategy_packet("gap_and_go", snap)
    ej = packet["seats"]["execution_judge"]
    assert ej["execution_ready"] is True
    assert packet["seats"]["governor"]["governor_action"] == "modulate"


def test_gap_and_go_blocks_when_below_emas():
    snap = _good_gap_and_go_snapshot(
        strategy="gap_and_go", price_above_emas=False,
    )
    packet = build_strategy_packet("gap_and_go", snap)
    ej = packet["seats"]["execution_judge"]
    assert ej["execution_ready"] is False
    assert ej["execution_checks"]["above_emas"] is False
    # Adversary should flag the daily trend issue.
    adv = packet["seats"]["adversary"]
    assert "daily_trend_against_strategy" in adv["objections"]


def test_gap_and_go_adversary_attacks_small_gap():
    snap = _good_gap_and_go_snapshot(strategy="gap_and_go", gap_pct=12)
    packet = build_strategy_packet("gap_and_go", snap)
    adv = packet["seats"]["adversary"]
    assert "gap_too_small_for_gap_and_go" in adv["objections"]


# ─── micro_pullback behavior ─────────────────────────────────────────

def test_micro_pullback_clean_setup_ready():
    snap = _good_micro_pullback_snapshot(strategy="micro_pullback")
    packet = build_strategy_packet("micro_pullback", snap)
    ej = packet["seats"]["execution_judge"]
    assert ej["execution_ready"] is True


def test_micro_pullback_dampens_when_no_stop_reference():
    """Doctrine (c, 2026-05-20): missing stop reference is a STRATEGY
    dampener, not a block. Governor never zeroes. RoadGuard / executor
    seat may still refuse to fire, but governor's role is sizing."""
    snap = _good_micro_pullback_snapshot(
        strategy="micro_pullback", pullback_low=None,
    )
    packet = build_strategy_packet("micro_pullback", snap)
    gov = packet["seats"]["governor"]
    assert gov["governor_action"] == "modulate"
    assert 0.0 < gov["risk_multiplier"] < 1.0


def test_micro_pullback_adversary_attacks_far_from_round_dollar():
    snap = _good_micro_pullback_snapshot(
        strategy="micro_pullback", near_half_or_whole_dollar=False,
    )
    packet = build_strategy_packet("micro_pullback", snap)
    adv = packet["seats"]["adversary"]
    assert "entry_not_near_half_or_whole_dollar" in adv["objections"]


# ─── SAC2024 refinement: pullback on non-leader ─────────────────────

def test_pullback_on_non_leader_does_not_score():
    """SAC2024: pullback pattern is invalid unless the stock is
    leading on gap or RVOL."""
    snap = _good_micro_pullback_snapshot(
        strategy="micro_pullback",
        gap_pct=3,             # not a gapper
        relative_volume=2,     # not high RVOL
        # everything else clean
    )
    packet = build_strategy_packet("micro_pullback", snap)
    labels = set(packet["base_labels"]["labels"])
    assert "VALID_PULLBACK_PATTERN" not in labels
    assert "PULLBACK_PATTERN_ON_NON_LEADER" in labels
    # Adversary catches it
    assert "pullback_on_non_leading_stock" in (
        packet["seats"]["adversary"]["objections"]
    )


def test_pullback_on_leading_gapper_still_scores():
    snap = _good_micro_pullback_snapshot(
        strategy="micro_pullback", gap_pct=25, relative_volume=2,
    )
    packet = build_strategy_packet("micro_pullback", snap)
    labels = set(packet["base_labels"]["labels"])
    assert "VALID_PULLBACK_PATTERN" in labels
    assert "PULLBACK_PATTERN_ON_NON_LEADER" not in labels


# ─── shape parity with existing pipeline ────────────────────────────

def test_strategy_packet_uses_same_role_keyed_shape():
    """Existing audit / scorecard / auto-retire layers expect the same
    seat shape as the generic doctrine. Both strategies must comply."""
    for strategy in ("gap_and_go", "micro_pullback"):
        snap_builder = (
            _good_gap_and_go_snapshot if strategy == "gap_and_go"
            else _good_micro_pullback_snapshot
        )
        packet = build_strategy_packet(strategy, snap_builder(strategy=strategy))
        assert packet["event_type"] == "BRAIN_DOCTRINE_SIDECAR_PACKET"
        assert packet["lane"] == "equity"
        for role in ("strategist", "adversary", "governor", "execution_judge"):
            seat = packet["seats"][role]
            assert seat["role"] == role
            assert seat["may_execute"] is False
            assert "seat" in seat
            assert "holder" in seat
