"""Tests for the role-keyed brain doctrine sidecar layer.

Doctrine pins exercised here:
    * Role-keyed shape — `seats: {strategist, adversary, governor,
      execution_judge}` — NOT brain-keyed.
    * Each seat carries `seat` (the roster seat name) and `holder`
      (the brain occupying that seat at packet build).
    * Restrictions live on the SEAT, not the brain. Every seat in
      every packet pins `may_execute=False`. Execution-judge also
      pins `may_create_direction=False` and
      `requires_existing_trade_intent=True`.
    * The labeler is pure (no DB, no async). The packet builder
      accepts an optional `seat_holders` map so it can stay sync.
"""
from __future__ import annotations

from shared.doctrine.base_labels import build_doctrine_labels
from shared.doctrine.brain_sidecars import build_all_brain_doctrine_packets


def _good_snapshot(**overrides):
    base = {
        "lane": "equity",
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
        "lane": "equity",
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
    doctrine = build_doctrine_labels(_good_snapshot())
    assert 0.0 <= doctrine.score <= 1.0


# ─── packet shape: role-keyed with seat + holder ──────────────────────

def test_packet_top_level_shape():
    packet = build_all_brain_doctrine_packets(_good_snapshot())
    assert packet["event_type"] == "BRAIN_DOCTRINE_SIDECAR_PACKET"
    assert packet["doctrine_version"] == "small_account_sidecar_v1"
    assert packet["lane"] == "equity"
    assert "base_labels" in packet
    assert set(packet["seats"].keys()) == {
        "strategist", "adversary", "governor", "execution_judge",
    }


def test_each_seat_has_seat_and_holder_fields():
    packet = build_all_brain_doctrine_packets(_good_snapshot())
    expected_seats = {
        "strategist": "decider",
        "adversary": "opponent",
        "governor": "governor",
        "execution_judge": "executor",
    }
    for role, expected_seat_name in expected_seats.items():
        s = packet["seats"][role]
        assert s["role"] == role, f"{role} missing 'role'"
        assert s["seat"] == expected_seat_name, f"{role} expected seat={expected_seat_name}, got {s.get('seat')}"
        assert "holder" in s, f"{role} missing 'holder'"


def test_packet_records_holder_when_provided():
    holders = {"decider": "alpha", "opponent": "redeye", "governor": "chevelle", "executor": "redeye"}
    packet = build_all_brain_doctrine_packets(_good_snapshot(), seat_holders=holders)
    assert packet["seats"]["strategist"]["holder"] == "alpha"
    assert packet["seats"]["adversary"]["holder"] == "redeye"
    assert packet["seats"]["governor"]["holder"] == "chevelle"
    assert packet["seats"]["execution_judge"]["holder"] == "redeye"


def test_packet_holder_none_when_no_holders_passed():
    packet = build_all_brain_doctrine_packets(_good_snapshot())
    for seat in packet["seats"].values():
        assert seat["holder"] is None


# ─── safety pins: restrictions live on the seat ──────────────────────

def test_every_seat_pins_may_execute_false():
    packet = build_all_brain_doctrine_packets(_good_snapshot())
    for seat in packet["seats"].values():
        assert seat["may_execute"] is False


def test_execution_judge_seat_pins_no_direction_creation():
    packet = build_all_brain_doctrine_packets(_good_snapshot())
    ej = packet["seats"]["execution_judge"]
    assert ej["may_create_direction"] is False
    assert ej["requires_existing_trade_intent"] is True


def test_non_execution_seats_pin_no_direction_override():
    packet = build_all_brain_doctrine_packets(_good_snapshot())
    for role in ("strategist", "adversary", "governor"):
        assert packet["seats"][role]["may_override_direction"] is False


# ─── role-flavored logic ─────────────────────────────────────────────

def test_strategist_lifts_conviction_on_a_quality():
    packet = build_all_brain_doctrine_packets(_good_snapshot())
    assert packet["seats"]["strategist"]["conviction_delta"] > 0


def test_strategist_drops_conviction_when_no_news():
    packet = build_all_brain_doctrine_packets(_good_snapshot(has_news=False))
    assert packet["seats"]["strategist"]["conviction_delta"] < 0.18


def test_adversary_objects_on_bad_setup():
    packet = build_all_brain_doctrine_packets(_bad_snapshot())
    adv = packet["seats"]["adversary"]
    assert adv["challenge_required"] is True
    assert "weak_market_regime" in adv["objections"]
    assert "spread_risk" in adv["objections"]


def test_adversary_quiet_on_clean_setup():
    packet = build_all_brain_doctrine_packets(_good_snapshot())
    assert len(packet["seats"]["adversary"]["objections"]) <= 1


def test_governor_blocks_after_three_losses():
    packet = build_all_brain_doctrine_packets(
        _good_snapshot(consecutive_losses=3, daily_pnl=-25),
    )
    gov = packet["seats"]["governor"]
    assert gov["governor_action"] == "block"
    assert gov["risk_multiplier"] == 0.0
    assert "three_consecutive_losses" in gov["block_reasons"]


def test_governor_blocks_on_daily_max_loss():
    packet = build_all_brain_doctrine_packets(
        _good_snapshot(consecutive_losses=0, daily_pnl=-150),
    )
    gov = packet["seats"]["governor"]
    assert gov["governor_action"] == "block"
    assert "daily_max_loss_reached" in gov["block_reasons"]


def test_governor_modulates_on_b_quality():
    packet = build_all_brain_doctrine_packets(_good_snapshot(relative_volume=1))
    assert packet["seats"]["governor"]["risk_multiplier"] < 1.0


def test_execution_judge_ready_on_clean_setup():
    packet = build_all_brain_doctrine_packets(_good_snapshot())
    ej = packet["seats"]["execution_judge"]
    assert ej["execution_ready"] is True
    assert all(ej["execution_checks"].values())


def test_execution_judge_not_ready_on_bad_setup():
    packet = build_all_brain_doctrine_packets(_bad_snapshot())
    assert packet["seats"]["execution_judge"]["execution_ready"] is False
