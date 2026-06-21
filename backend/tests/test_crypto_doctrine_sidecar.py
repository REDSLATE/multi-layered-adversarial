"""Tests for crypto-lane doctrine sidecar + lane router.

Twin doctrine pin: equity doctrine and crypto doctrine NEVER share
labels. Crypto rejects equity-lane snapshots loudly. The lane router
fans to the correct twin and produces an UNKNOWN_LANE_REJECT packet
for anything else.
"""
from __future__ import annotations

from shared.crypto.doctrine.crypto_brain_sidecars import (
    build_crypto_brain_doctrine_packet,
)
from shared.crypto.doctrine.crypto_labels import label_crypto_snapshot
from shared.doctrine.lane_doctrine_router import (
    build_lane_doctrine_packet,
    hoist_packet_audit_fields,
)


# ─── labeler ─────────────────────────────────────────────────────────

def test_crypto_labeler_rejects_equity_lane():
    """Defense-in-depth: even if a caller bypasses the router, the
    crypto labeler must refuse equity-lane snapshots."""
    labels = label_crypto_snapshot({"lane": "equity", "symbol": "AAPL"})
    assert labels.quality == "REJECT"
    assert "WRONG_LANE" in labels.labels


def test_crypto_good_snapshot_scores_a_quality():
    labels = label_crypto_snapshot({
        "lane": "crypto",
        "symbol": "BTC/USD",
        "volume_24h_usd": 500_000_000,
        "spread_bps": 10,
        "volatility_1h": 0.02,
        "trend_strength": 0.8,
        "funding_rate": 0.0001,
        "open_interest_change_pct": 5,
        "liquidation_imbalance": 0.2,
        "btc_regime_alignment": 0.8,
        "exchange_liquidity_score": 0.9,
    })
    assert labels.score >= 0.80
    assert labels.quality == "A_QUALITY"


def test_crypto_bad_snapshot_rejected():
    labels = label_crypto_snapshot({
        "lane": "crypto",
        "symbol": "DOGE/USD",
        "volume_24h_usd": 1_000_000,
        "spread_bps": 250,
        "volatility_1h": 0.001,
        "funding_rate": 0.002,
        "liquidation_imbalance": 1.0,
    })
    assert labels.quality in {"C_QUALITY", "REJECT"}
    assert "WIDE_SPREAD" in labels.labels
    assert "DEAD_VOL" in labels.labels


# ─── crypto brain packet ─────────────────────────────────────────────

def test_crypto_brain_packet_has_no_execution_authority():
    packet = build_crypto_brain_doctrine_packet({
        "lane": "crypto",
        "symbol": "BTC/USD",
        "existing_intent": True,
        "volume_24h_usd": 500_000_000,
        "spread_bps": 10,
        "exchange_liquidity_score": 0.9,
        "trend_strength": 0.8,
    })
    for seat in packet["seats"].values():
        assert seat["may_execute"] is False
    assert packet["seats"]["execution_judge"]["may_create_direction"] is False
    assert packet["lane"] == "crypto"


def test_chevelle_dampens_on_wide_spread():
    """Doctrine (c, 2026-05-20): Chevelle dampens wide spread, never
    hard-blocks. RoadGuard kills truly unsafe markets at the
    `roadguard_spread_floor` gate in execution.py."""
    packet = build_crypto_brain_doctrine_packet({
        "lane": "crypto",
        "symbol": "DOGE/USD",
        "existing_intent": True,
        "spread_bps": 200,
    })
    gov = packet["seats"]["governor"]
    assert gov["block_reasons"] == []
    assert gov["governor_action"] == "modulate"
    dampener_names = [n for (n, _m) in gov["dampeners"]]
    assert "WIDE_SPREAD" in dampener_names
    assert gov["risk_multiplier"] < 1.0


def test_chevelle_dampens_on_three_consecutive_losses():
    packet = build_crypto_brain_doctrine_packet({
        "lane": "crypto",
        "symbol": "ETH/USD",
        "existing_intent": True,
        "consecutive_losses": 3,
    })
    gov = packet["seats"]["governor"]
    assert gov["block_reasons"] == []
    assert gov["governor_action"] == "modulate"
    dampener_names = [n for (n, _m) in gov["dampeners"]]
    assert "THREE_CONSECUTIVE_LOSSES" in dampener_names


def test_chevelle_dampens_on_daily_loss_limit():
    packet = build_crypto_brain_doctrine_packet({
        "lane": "crypto",
        "symbol": "ETH/USD",
        "existing_intent": True,
        "daily_pnl_usd": -200,
    })
    gov = packet["seats"]["governor"]
    assert gov["block_reasons"] == []
    assert gov["governor_action"] == "modulate"
    dampener_names = [n for (n, _m) in gov["dampeners"]]
    assert "DAILY_LOSS_LIMIT" in dampener_names


def test_camaro_not_ready_without_existing_intent():
    packet = build_crypto_brain_doctrine_packet({
        "lane": "crypto",
        "symbol": "BTC/USD",
        "existing_intent": False,
        "volume_24h_usd": 500_000_000,
        "spread_bps": 10,
        "exchange_liquidity_score": 0.9,
        "trend_strength": 0.8,
    })
    assert packet["seats"]["execution_judge"]["execution_ready"] is False


def test_redeye_objections_on_bad_setup():
    packet = build_crypto_brain_doctrine_packet({
        "lane": "crypto",
        "symbol": "ALT/USD",
        "spread_bps": 200,         # WIDE_SPREAD
        "funding_rate": 0.002,     # FUNDING_CROWDED
        "liquidation_imbalance": 1.0,  # LIQUIDATION_RISK
    })
    objections = packet["seats"]["adversary"]["objections"]
    assert any("spread" in o for o in objections)
    assert any("funding" in o for o in objections)


def test_crypto_packet_records_seat_holders():
    holders = {
        "crypto_strategist": "alpha",
        "crypto_auditor": "redeye",
        "crypto_governor": "chevelle",
        "crypto": "redeye",   # crypto executor seat
    }
    packet = build_crypto_brain_doctrine_packet(
        {"lane": "crypto", "symbol": "BTC/USD", "existing_intent": True},
        seat_holders=holders,
    )
    assert packet["seats"]["strategist"]["holder"] == "alpha"
    assert packet["seats"]["strategist"]["seat"] == "crypto_strategist"
    assert packet["seats"]["adversary"]["holder"] == "redeye"
    assert packet["seats"]["adversary"]["seat"] == "crypto_auditor"
    assert packet["seats"]["governor"]["holder"] == "chevelle"
    assert packet["seats"]["execution_judge"]["holder"] == "redeye"
    assert packet["seats"]["execution_judge"]["seat"] == "crypto"


def test_brain_can_hold_seats_in_both_lanes_simultaneously():
    """Doctrine: brains can occupy multiple seats ACROSS lanes. Verify
    the same brain can show up as a holder in both an equity packet
    and a crypto packet built from the same roster state."""
    eq_holders = {"strategist": "alpha", "executor": "alpha"}
    crypto_holders = {"crypto_strategist": "alpha", "crypto": "alpha"}

    eq = build_lane_doctrine_packet(
        {"lane": "equity", "symbol": "NVDA",
         "price": 7.5, "gap_pct": 22, "relative_volume": 8,
         "has_news": True, "float_millions": 10, "pattern": "pullback",
         "market_regime": "strong", "spread_bps": 40},
        seat_holders=eq_holders,
    )
    cr = build_lane_doctrine_packet(
        {"lane": "crypto", "symbol": "BTC/USD", "existing_intent": True},
        seat_holders=crypto_holders,
    )
    assert eq["seats"]["strategist"]["holder"] == "alpha"
    assert eq["seats"]["execution_judge"]["holder"] == "alpha"
    assert cr["seats"]["strategist"]["holder"] == "alpha"
    assert cr["seats"]["execution_judge"]["holder"] == "alpha"


# ─── lane router ─────────────────────────────────────────────────────

def test_router_sends_crypto_to_crypto_packet():
    packet = build_lane_doctrine_packet({
        "lane": "crypto",
        "symbol": "ETH/USD",
        "existing_intent": True,
    })
    assert packet["lane"] == "crypto"
    assert packet["doctrine_version"] == "crypto_sidecar_v1"
    assert "seats" in packet
    assert set(packet["seats"].keys()) == {
        "strategist", "adversary", "governor", "execution_judge",
    }


def test_router_sends_equity_to_equity_packet():
    """2026-02-20 directive: equity defaults to LARGE-cap doctrine
    unless the snapshot explicitly opts into small-cap via
    `strategy` or `market_cap_band`. This snapshot leaves both
    blank → must route to `large_cap_equity_v1`."""
    packet = build_lane_doctrine_packet({
        "lane": "equity",
        "symbol": "NVDA",
        "price": 7.5, "gap_pct": 22, "relative_volume": 8,
        "has_news": True, "float_millions": 10, "pattern": "pullback",
        "market_regime": "strong", "spread_bps": 40,
    })
    assert packet["doctrine_version"] == "large_cap_equity_v1"
    assert packet["lane"] == "equity"
    assert "seats" in packet


def test_router_sends_small_cap_strategy_to_small_account_sidecar():
    """When the snapshot explicitly opts into a small-cap strategy
    (`gap_and_go` here), the router MUST honour that and dispatch
    to the small-account sidecar — not the large-cap default."""
    packet = build_lane_doctrine_packet({
        "lane": "equity",
        "symbol": "TINY",
        "strategy": "gap_and_go",
        "market_cap_band": "small",
        "price": 7.5, "gap_pct": 22, "relative_volume": 8,
        "has_news": True, "float_millions": 10, "pattern": "pullback",
        "market_regime": "strong", "spread_bps": 40,
    })
    assert packet["lane"] == "equity"
    assert "seats" in packet
    # The strategy doctrine OR the small-account sidecar is acceptable
    # — what matters is that we did NOT fall through to large-cap.
    assert packet["doctrine_version"] != "large_cap_equity_v1"


def test_router_rejects_unknown_lane():
    """TWO LANES ONLY. Anything else hard-rejects."""
    packet = build_lane_doctrine_packet({
        "lane": "options",  # not equity, not crypto
        "symbol": "SPY",
    })
    assert packet["doctrine_version"] == "unknown_lane_reject_v1"
    assert packet["base_labels"]["quality"] == "REJECT"
    assert "UNKNOWN_LANE" in packet["base_labels"]["labels"]
    assert packet["seats"] == {}


def test_router_rejects_missing_lane():
    packet = build_lane_doctrine_packet({"symbol": "X"})
    assert packet["doctrine_version"] == "unknown_lane_reject_v1"


# ─── audit-field hoister handles role-keyed shape ────────────────────

def test_hoist_works_for_crypto_packet():
    crypto_pkt = build_crypto_brain_doctrine_packet({
        "lane": "crypto",
        "symbol": "BTC/USD",
        "existing_intent": True,
        "volume_24h_usd": 500_000_000,
        "spread_bps": 10,
        "exchange_liquidity_score": 0.9,
        "trend_strength": 0.8,
    })
    hoisted = hoist_packet_audit_fields(crypto_pkt)
    assert hoisted["quality"] in {"A_QUALITY", "B_QUALITY"}
    assert hoisted["camaro_execution_ready"] is True
    assert hoisted["chevelle_governor_action"] in {"block", "modulate"}
    assert isinstance(hoisted["redeye_challenge_required"], bool)


def test_hoist_works_for_equity_packet():
    # Under large-cap doctrine (the 2026-02-20 default for equity),
    # SPREAD_TIGHT requires spread_bps ≤ 25. The original test used
    # 40 bps which routes to SPREAD_TOO_WIDE → execution_ready=False
    # — that's a snapshot quality problem, not a hoister bug.
    eq_pkt = build_lane_doctrine_packet({
        "lane": "equity",
        "symbol": "NVDA",
        "price": 7.5, "gap_pct": 22, "relative_volume": 8,
        "has_news": True, "float_millions": 10, "pattern": "pullback",
        "market_regime": "strong", "spread_bps": 15,
    })
    hoisted = hoist_packet_audit_fields(eq_pkt)
    assert hoisted["quality"] == "A_QUALITY"
    assert hoisted["camaro_execution_ready"] is True
    assert hoisted["chevelle_governor_action"] == "modulate"
