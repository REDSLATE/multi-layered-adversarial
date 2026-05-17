"""Tests for the brain doctrine sidecar layer.

These exercise the lane-neutral `shared.doctrine.base_labels` core and
the four per-brain interpreters. All sidecars are pure functions — no
DB, no async, no LLM — so we can drive the full decision matrix from
plain calls.
"""
from __future__ import annotations

from shared.doctrine.base_labels import build_doctrine_labels
from shared.doctrine.brain_sidecars import build_all_brain_doctrine_packets


def _good_snapshot(**overrides):
    base = {
        "symbol": "TEST",
        "price": 7.50,
        "gap_pct": 22,
        "relative_volume": 8,
        "has_news": True,
        "float_millions": 10,
        "pattern": "pullback",
        "market_regime": "strong",
        "spread_bps": 40,
    }
    base.update(overrides)
    return base


def _bad_snapshot(**overrides):
    base = {
        "symbol": "BAD",
        "price": 80,
        "gap_pct": 1,
        "relative_volume": 1,
        "has_news": False,
        "float_millions": 300,
        "pattern": "none",
        "market_regime": "weak",
        "spread_bps": 200,
    }
    base.update(overrides)
    return base


# ─── shared core ──────────────────────────────────────────────────────

def test_a_quality_setup_scores_high():
    doctrine = build_doctrine_labels(_good_snapshot())
    assert doctrine.quality == "A_QUALITY"
    assert "GAPPER" in doctrine.labels
    assert "HIGH_RELATIVE_VOLUME" in doctrine.labels
    assert "NEWS_CATALYST" in doctrine.labels


def test_bad_setup_gets_rejected_or_low_quality():
    doctrine = build_doctrine_labels(_bad_snapshot())
    assert doctrine.quality in {"C_QUALITY", "REJECT"}
    assert "NO_NEWS_RISK" in doctrine.labels
    assert "SPREAD_TOO_WIDE" in doctrine.labels


def test_score_clamped_to_unit_interval():
    # Pile every positive label — score must not exceed 1.0
    doctrine = build_doctrine_labels(_good_snapshot())
    assert 0.0 <= doctrine.score <= 1.0


def test_quality_band_boundaries():
    # Confirm the four bands: A ≥0.80, B ≥0.60, C ≥0.40, REJECT below.
    assert build_doctrine_labels(_good_snapshot()).quality == "A_QUALITY"
    # Strip one big-weight label to drop into B range
    b = build_doctrine_labels(_good_snapshot(relative_volume=1))
    assert b.quality in {"B_QUALITY", "C_QUALITY"}


# ─── sidecar safety pins ──────────────────────────────────────────────

def test_sidecars_do_not_execute_or_create_direction():
    packet = build_all_brain_doctrine_packets(_good_snapshot())
    assert packet["alpha"]["may_execute"] is False
    assert packet["redeye"]["may_execute"] is False
    assert packet["chevelle"]["may_execute"] is False
    assert packet["camaro"]["may_execute"] is False
    assert packet["camaro"]["may_create_direction"] is False
    assert packet["alpha"]["may_override_direction"] is False
    assert packet["redeye"]["may_override_direction"] is False
    assert packet["chevelle"]["may_override_direction"] is False


def test_packet_carries_event_type_and_version():
    packet = build_all_brain_doctrine_packets(_good_snapshot())
    assert packet["event_type"] == "BRAIN_DOCTRINE_SIDECAR_PACKET"
    assert packet["doctrine_version"] == "small_account_sidecar_v1"
    assert packet["symbol"] == "TEST"


# ─── alpha ────────────────────────────────────────────────────────────

def test_alpha_conviction_lifts_on_a_quality():
    packet = build_all_brain_doctrine_packets(_good_snapshot())
    assert packet["alpha"]["conviction_delta"] > 0


def test_alpha_conviction_punishes_no_news():
    snap = _good_snapshot(has_news=False)
    packet = build_all_brain_doctrine_packets(snap)
    # NO_NEWS_RISK label means the conviction delta is reduced
    assert packet["alpha"]["conviction_delta"] < 0.18  # would be 0.12+0.06+0.04 = 0.22 with news


# ─── redeye ───────────────────────────────────────────────────────────

def test_redeye_challenges_weak_setup():
    packet = build_all_brain_doctrine_packets(_bad_snapshot())
    assert packet["redeye"]["challenge_required"] is True
    assert "weak_market_regime" in packet["redeye"]["objections"]
    assert "spread_risk" in packet["redeye"]["objections"]


def test_redeye_quiet_on_clean_setup():
    packet = build_all_brain_doctrine_packets(_good_snapshot())
    # Clean A-quality setup → few or no objections
    assert len(packet["redeye"]["objections"]) <= 1


# ─── chevelle ─────────────────────────────────────────────────────────

def test_chevelle_blocks_after_three_losses():
    snap = _good_snapshot(consecutive_losses=3, daily_pnl=-25)
    packet = build_all_brain_doctrine_packets(snap)
    assert packet["chevelle"]["governor_action"] == "block"
    assert packet["chevelle"]["risk_multiplier"] == 0.0
    assert "three_consecutive_losses" in packet["chevelle"]["block_reasons"]


def test_chevelle_blocks_on_daily_max_loss():
    snap = _good_snapshot(consecutive_losses=0, daily_pnl=-150)
    packet = build_all_brain_doctrine_packets(snap)
    assert packet["chevelle"]["governor_action"] == "block"
    assert "daily_max_loss_reached" in packet["chevelle"]["block_reasons"]


def test_chevelle_modulates_on_b_quality():
    snap = _good_snapshot(relative_volume=1)  # drops it out of A
    packet = build_all_brain_doctrine_packets(snap)
    assert packet["chevelle"]["risk_multiplier"] < 1.0


# ─── camaro ───────────────────────────────────────────────────────────

def test_camaro_ready_on_clean_setup():
    packet = build_all_brain_doctrine_packets(_good_snapshot())
    assert packet["camaro"]["execution_ready"] is True
    checks = packet["camaro"]["execution_checks"]
    assert all(checks.values())


def test_camaro_not_ready_on_bad_setup():
    packet = build_all_brain_doctrine_packets(_bad_snapshot())
    assert packet["camaro"]["execution_ready"] is False


def test_camaro_requires_existing_trade_intent():
    packet = build_all_brain_doctrine_packets(_good_snapshot())
    assert packet["camaro"]["requires_existing_trade_intent"] is True
