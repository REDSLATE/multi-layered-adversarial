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


# ─── tier upgrades from source material (Toolkit + Tech Analysis v3) ──

def test_sweet_spot_price_tier_label():
    """$5-$10 sweet-spot tier per 2025 Small Account Tool Kit p.3."""
    doctrine = build_doctrine_labels(_good_snapshot(price=7.50))
    assert "SMALL_ACCOUNT_PRICE_VALID" in doctrine.labels
    assert "SWEET_SPOT_PRICE" in doctrine.labels
    # $15 is valid but outside sweet spot
    doctrine2 = build_doctrine_labels(_good_snapshot(price=15.0))
    assert "SMALL_ACCOUNT_PRICE_VALID" in doctrine2.labels
    assert "SWEET_SPOT_PRICE" not in doctrine2.labels


def test_strong_gapper_tier_label():
    """≥20% gap tier upgrade per Technical Analysis v3 Gap-and-Go."""
    doctrine = build_doctrine_labels(_good_snapshot(gap_pct=22))
    assert "GAPPER" in doctrine.labels
    assert "STRONG_GAPPER" in doctrine.labels
    doctrine2 = build_doctrine_labels(_good_snapshot(gap_pct=12))
    assert "GAPPER" in doctrine2.labels
    assert "STRONG_GAPPER" not in doctrine2.labels


def test_ultra_low_float_tier_label():
    """<10M float tier upgrade per Toolkit 'cold market' threshold."""
    doctrine = build_doctrine_labels(_good_snapshot(float_millions=8))
    assert "LOW_FLOAT_SUPPLY_IMBALANCE" in doctrine.labels
    assert "ULTRA_LOW_FLOAT" in doctrine.labels
    doctrine2 = build_doctrine_labels(_good_snapshot(float_millions=15))
    assert "LOW_FLOAT_SUPPLY_IMBALANCE" in doctrine2.labels
    assert "ULTRA_LOW_FLOAT" not in doctrine2.labels


def test_bull_flag_and_flat_top_patterns_accepted():
    """Specific named patterns from Tech Analysis v3 accepted as valid."""
    for pat, named in (
        ("bull_flag", "BULL_FLAG_PATTERN"),
        ("flat_top_breakout", "FLAT_TOP_BREAKOUT_PATTERN"),
        ("micro_pullback", "MICRO_PULLBACK_PATTERN"),
    ):
        d = build_doctrine_labels(_good_snapshot(pattern=pat))
        assert "VALID_PULLBACK_PATTERN" in d.labels, pat
        assert named in d.labels, (pat, named)


def test_trading_window_label_when_hour_supplied():
    """7-11am EST prime window per Toolkit. Absence = informational."""
    d_in = build_doctrine_labels(_good_snapshot(hour_et=9))
    assert "TRADING_WINDOW_PRIME" in d_in.labels
    d_out = build_doctrine_labels(_good_snapshot(hour_et=14))
    assert "TRADING_WINDOW_OFF_HOURS" in d_out.labels
    # Missing hour_et → no window label at all
    d_missing = build_doctrine_labels(_good_snapshot())
    assert "TRADING_WINDOW_PRIME" not in d_missing.labels
    assert "TRADING_WINDOW_OFF_HOURS" not in d_missing.labels



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


def test_governor_dampens_after_three_losses():
    """Doctrine (c, 2026-05-20): consecutive losses dampen size; they
    no longer hard-block. RoadGuard owns hard kills."""
    packet = build_all_brain_doctrine_packets(
        _good_snapshot(consecutive_losses=3, daily_pnl=-25),
    )
    gov = packet["seats"]["governor"]
    assert gov["governor_action"] == "modulate"
    # Size is dampened but not zeroed
    assert 0.0 < gov["risk_multiplier"] < 1.0


def test_governor_dampens_on_daily_max_loss():
    """Doctrine (c): daily loss limit dampens severely (0.25× floor)
    but governor never emits a hard block."""
    packet = build_all_brain_doctrine_packets(
        _good_snapshot(consecutive_losses=0, daily_pnl=-150),
    )
    gov = packet["seats"]["governor"]
    assert gov["governor_action"] == "modulate"
    assert 0.0 < gov["risk_multiplier"] < 1.0


def test_governor_modulates_on_b_quality():
    # Deliberately drop two factors (RVOL miss + no news) to land cleanly
    # in B_QUALITY territory regardless of tier-upgrade nudges. The
    # previous one-override version floated on FP precision (0.7999…)
    # which masked the doctrinal intent.
    packet = build_all_brain_doctrine_packets(
        _good_snapshot(relative_volume=1, has_news=False),
    )
    assert packet["base_labels"]["quality"] == "B_QUALITY"
    assert packet["seats"]["governor"]["risk_multiplier"] < 1.0


def test_execution_judge_ready_on_clean_setup():
    packet = build_all_brain_doctrine_packets(_good_snapshot())
    ej = packet["seats"]["execution_judge"]
    assert ej["execution_ready"] is True
    assert all(ej["execution_checks"].values())


def test_execution_judge_not_ready_on_bad_setup():
    packet = build_all_brain_doctrine_packets(_bad_snapshot())
    assert packet["seats"]["execution_judge"]["execution_ready"] is False
