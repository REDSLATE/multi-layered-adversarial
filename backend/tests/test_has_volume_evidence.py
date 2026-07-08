"""Tests for the dual-path `has_volume_evidence` gate in large-cap doctrine.

Doctrine (2026-02-20 operator directive):

    Path A (strict):
        RVOL >= 1.5 → full pass. Governor sizing unchanged (full).

    Path B (accelerating toehold):
        RVOL >= 0.9 AND rvol_acceleration >= 0.25 AND
        trend_score > 0 AND vwap_distance_pct >= 0
        → toehold pass. Governor clamps to 0.25× sizing.

    Otherwise → block execution (has_volume=False).
"""
from __future__ import annotations

import pytest

from shared.doctrine.large_cap_doctrine import (
    build_large_cap_doctrine_packet,
    has_volume_evidence,
)


# ─────────────────────── unit tests: pure helper ───────────────────────

def test_path_a_strict_elevated_rvol():
    ok, reason = has_volume_evidence({"relative_volume": 1.5})
    assert ok is True
    assert reason == "ELEVATED_RELATIVE_VOLUME"


def test_path_a_strict_high_rvol():
    ok, reason = has_volume_evidence({"relative_volume": 3.5})
    assert ok is True
    assert reason == "ELEVATED_RELATIVE_VOLUME"


def test_path_b_toehold_pass_all_conditions_met():
    snap = {
        "relative_volume": 0.9,
        "rvol_acceleration": 0.25,
        "trend_score": 0.01,
        "vwap_distance_pct": 0.0,
    }
    ok, reason = has_volume_evidence(snap)
    assert ok is True
    assert reason == "RVOL_ACCELERATING_CONFIRMED"


def test_path_b_all_conditions_generous():
    snap = {
        "relative_volume": 1.2,
        "rvol_acceleration": 0.4,
        "trend_score": 0.02,
        "vwap_distance_pct": 0.3,
    }
    ok, reason = has_volume_evidence(snap)
    assert ok is True
    assert reason == "RVOL_ACCELERATING_CONFIRMED"


def test_path_b_fails_rvol_below_09():
    snap = {
        "relative_volume": 0.85,
        "rvol_acceleration": 0.5,
        "trend_score": 0.02,
        "vwap_distance_pct": 0.3,
    }
    ok, reason = has_volume_evidence(snap)
    assert ok is False
    assert reason == "VOLUME_NOT_CONFIRMED"


def test_path_b_fails_accel_below_025():
    snap = {
        "relative_volume": 1.0,
        "rvol_acceleration": 0.20,
        "trend_score": 0.02,
        "vwap_distance_pct": 0.3,
    }
    ok, _ = has_volume_evidence(snap)
    assert ok is False


def test_path_b_fails_trend_zero_or_negative():
    snap = {
        "relative_volume": 1.0,
        "rvol_acceleration": 0.30,
        "trend_score": 0.0,
        "vwap_distance_pct": 0.3,
    }
    ok, _ = has_volume_evidence(snap)
    assert ok is False

    snap["trend_score"] = -0.01
    ok, _ = has_volume_evidence(snap)
    assert ok is False


def test_path_b_fails_below_vwap():
    snap = {
        "relative_volume": 1.0,
        "rvol_acceleration": 0.30,
        "trend_score": 0.02,
        "vwap_distance_pct": -0.5,
    }
    ok, _ = has_volume_evidence(snap)
    assert ok is False


def test_missing_fields_default_to_zero_and_fail():
    ok, reason = has_volume_evidence({})
    assert ok is False
    assert reason == "VOLUME_NOT_CONFIRMED"


def test_none_values_are_safe():
    snap = {
        "relative_volume": None,
        "rvol_acceleration": None,
        "trend_score": None,
        "vwap_distance_pct": None,
    }
    ok, _ = has_volume_evidence(snap)
    assert ok is False


def test_strict_path_wins_when_both_qualify():
    # RVOL >= 1.5 always returns strict, even if Path B would also pass.
    snap = {
        "relative_volume": 1.6,
        "rvol_acceleration": 0.4,
        "trend_score": 0.02,
        "vwap_distance_pct": 0.5,
    }
    _, reason = has_volume_evidence(snap)
    assert reason == "ELEVATED_RELATIVE_VOLUME"


# ─────────────── integration: full doctrine packet ───────────────

def _base_snapshot(**overrides):
    """A minimum-complete large-cap snapshot that will not hit NO_DATA."""
    snap = {
        "symbol": "NVDA",
        "gap_pct": 0.0,
        "relative_volume": 0.0,
        "has_news": False,
        "market_regime": "neutral",
        "spread_bps": 8.0,
        "spread_source": "webull_l1",
        "spread_quality": "live",
        "pattern": None,
        "price": 500.0,
    }
    snap.update(overrides)
    return snap


def test_packet_toehold_pass_execution_ready_and_governor_clamped():
    snap = _base_snapshot(
        relative_volume=1.0,
        rvol_acceleration=0.3,
        trend_score=0.01,
        vwap_distance_pct=0.4,
    )
    pkt = build_large_cap_doctrine_packet(snap)

    assert "RVOL_ACCELERATING_CONFIRMED" in pkt["base_labels"]["labels"]
    assert "ELEVATED_RELATIVE_VOLUME" not in pkt["base_labels"]["labels"]
    assert "VOLUME_NOT_CONFIRMED" not in pkt["base_labels"]["labels"]

    exec_checks = pkt["seats"]["execution_judge"]["execution_checks"]
    assert exec_checks["has_volume"] is True

    # Governor toehold clamp: 0.25× applied
    assert pkt["seats"]["governor"]["risk_multiplier"] <= 0.25


def test_packet_strict_pass_execution_ready_full_sizing():
    snap = _base_snapshot(relative_volume=2.0)
    pkt = build_large_cap_doctrine_packet(snap)

    assert "ELEVATED_RELATIVE_VOLUME" in pkt["base_labels"]["labels"]
    assert "RVOL_ACCELERATING_CONFIRMED" not in pkt["base_labels"]["labels"]

    exec_checks = pkt["seats"]["execution_judge"]["execution_checks"]
    assert exec_checks["has_volume"] is True

    # Governor NOT clamped by RVOL_ACCELERATING toehold — should be
    # meaningfully higher than 0.25 for a B/C-quality snapshot.
    assert pkt["seats"]["governor"]["risk_multiplier"] > 0.25


def test_packet_volume_not_confirmed_blocks_execution():
    snap = _base_snapshot(
        relative_volume=0.5,
        rvol_acceleration=0.1,
        trend_score=0.005,
        vwap_distance_pct=-0.2,
    )
    pkt = build_large_cap_doctrine_packet(snap)

    assert "VOLUME_NOT_CONFIRMED" in pkt["base_labels"]["labels"]
    exec_checks = pkt["seats"]["execution_judge"]["execution_checks"]
    assert exec_checks["has_volume"] is False
    assert pkt["seats"]["execution_judge"]["execution_ready"] is False


def test_packet_adversary_no_volume_objection_on_toehold_pass():
    snap = _base_snapshot(
        relative_volume=1.0,
        rvol_acceleration=0.3,
        trend_score=0.01,
        vwap_distance_pct=0.4,
    )
    pkt = build_large_cap_doctrine_packet(snap)
    objs = pkt["seats"]["adversary"]["objections"]
    assert "rvol_too_quiet_for_directional" not in objs


def test_packet_adversary_objects_when_volume_not_confirmed():
    snap = _base_snapshot(
        relative_volume=0.5,
        rvol_acceleration=0.0,
        trend_score=0.0,
        vwap_distance_pct=-1.0,
    )
    pkt = build_large_cap_doctrine_packet(snap)
    objs = pkt["seats"]["adversary"]["objections"]
    assert "rvol_too_quiet_for_directional" in objs


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
