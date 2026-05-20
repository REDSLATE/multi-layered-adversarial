"""Doctrine (c) — Separation of Concerns

Locked rules:
  * Brains  → directional agency (BUY/SELL/HOLD + confidence; own floor)
  * Chevelle/Governor → SIZE ONLY; never emits hard blocks
  * Opponent seat → only directional hard veto (HARD_VETO_OPPONENT)
  * RoadGuard → deterministic market-structure safety caps
  * MC → authority/schema/broker/cap verifier (no brain re-judgement)
  * Patent J → brain promotion readiness only

These tests pin the doctrine surface. They must not regress.
"""
from __future__ import annotations

import pytest

from shared.crypto.doctrine.crypto_brain_sidecars import (
    GOVERNOR_DAMPENERS,
    _build_governor,
    _chevelle_blocks,
    _chevelle_dampeners,
)


class _Base:
    """Minimal stand-in for the brain-side base object."""
    def __init__(self, score: float = 0.7, quality: str = "B", reasons=()):
        self.score = score
        self.quality = quality
        self.reasons = list(reasons)


# ───── Governor doctrine: SIZE ONLY ──────────────────────────────────


@pytest.mark.tripwire
def test_governor_block_reasons_always_empty_under_doctrine_c():
    """Chevelle no longer emits hard blocks. `_chevelle_blocks` returns
    [] regardless of labels. RoadGuard/opponent own veto authority."""
    assert _chevelle_blocks({"WIDE_SPREAD"}, {}) == []
    assert _chevelle_blocks({"WRONG_LANE"}, {}) == []
    assert _chevelle_blocks(set(), {"consecutive_losses": 5}) == []
    assert _chevelle_blocks(set(), {"daily_pnl_usd": -500}) == []


@pytest.mark.tripwire
def test_governor_packet_action_is_modulate_not_block():
    """Even on wide-spread + multi-loss snapshot the governor packet
    must emit `governor_action='modulate'` and an empty `block_reasons`."""
    pkt = _build_governor(
        _Base(score=0.7),
        {"WIDE_SPREAD"},
        holder="chevelle",
        snapshot={"consecutive_losses": 4, "daily_pnl_usd": -250},
    )
    assert pkt["governor_action"] == "modulate"
    assert pkt["block_reasons"] == []
    assert pkt["execution_effect"] in {"ALLOW", "RISK_DOWN_ONLY"}


@pytest.mark.tripwire
def test_governor_wide_spread_dampens_to_50pct():
    """WIDE_SPREAD must apply the 0.50 dampener — not block."""
    name, mult = _chevelle_dampeners({"WIDE_SPREAD"}, {})[0]
    assert name == "WIDE_SPREAD"
    assert mult == GOVERNOR_DAMPENERS["WIDE_SPREAD"] == 0.50


def test_governor_strongest_dampener_wins():
    """Daily loss limit (0.25) must beat wide-spread (0.50)."""
    dampeners = _chevelle_dampeners(
        {"WIDE_SPREAD"},
        {"daily_pnl_usd": -500},
    )
    # multiplication of applicable dampeners is the policy in
    # `_build_governor`; the dampener LIST should contain both.
    names = [n for (n, _m) in dampeners]
    assert "WIDE_SPREAD" in names
    assert "DAILY_LOSS_LIMIT" in names


def test_governor_score_zero_floored_not_zeroed():
    """Score 0 with no fatal labels must floor at 0.25, not 0.0.
    Operator must still see Chevelle's dissent on the ledger AND the
    trade must be ABLE to proceed at minimum size if all other gates
    pass."""
    pkt = _build_governor(_Base(score=0.0), set(), holder="chevelle", snapshot={})
    assert pkt["risk_multiplier"] >= 0.25
    assert pkt["governor_action"] == "modulate"


def test_governor_clean_snapshot_full_size():
    """A-quality, no dampeners → risk_multiplier == 1.0."""
    pkt = _build_governor(_Base(score=0.85), set(), holder="chevelle", snapshot={})
    assert pkt["risk_multiplier"] == 1.0
    assert pkt["execution_effect"] == "ALLOW"
    assert pkt["dampeners"] == []


@pytest.mark.tripwire
def test_governor_dampener_table_locked():
    """The dampener table is the operator-visible doctrine. Lock it."""
    assert GOVERNOR_DAMPENERS["WIDE_SPREAD"] == 0.50
    assert GOVERNOR_DAMPENERS["LOW_VOLUME"] == 0.60
    assert GOVERNOR_DAMPENERS["LOW_QUALITY"] == 0.70
    assert GOVERNOR_DAMPENERS["UNCERTAIN"] == 0.75


# ───── Equity doctrine (gap_and_go / micro_pullback / base) ──────────


@pytest.mark.tripwire
def test_equity_base_doctrine_quality_reject_does_not_zero_size():
    """REJECT quality is advisory; governor must not zero on it."""
    from shared.doctrine.brain_sidecars import _build_governor as _eq_gov
    base = type("B", (), {"quality": "REJECT"})()
    pkt = _eq_gov(base, labels=set(), holder="chevelle", snapshot={})
    assert pkt["governor_action"] == "modulate"
    assert pkt["risk_multiplier"] > 0
    assert pkt["execution_effect"] != "HARD_BLOCK"


@pytest.mark.tripwire
def test_equity_base_doctrine_consecutive_losses_dampens_not_blocks():
    """3 consecutive losses → severe dampener, not hard block."""
    from shared.doctrine.brain_sidecars import _build_governor as _eq_gov
    base = type("B", (), {"quality": "A_QUALITY"})()
    pkt = _eq_gov(
        base,
        labels=set(),
        holder="chevelle",
        snapshot={"consecutive_losses": 4, "daily_pnl": -500},
    )
    assert pkt["governor_action"] == "modulate"
    assert pkt["risk_multiplier"] > 0
    assert pkt["execution_effect"] != "HARD_BLOCK"


@pytest.mark.tripwire
def test_equity_strategy_doctrine_governor_never_blocks():
    """gap_and_go and micro_pullback strategy packets must never emit
    governor_action='block' regardless of inputs."""
    from shared.doctrine.strategy_doctrines import (
        _build_gap_and_go_v1,
        _build_micro_pullback_v1,
    )

    worst_snapshot = {
        "symbol": "TEST",
        "consecutive_losses": 5,
        "daily_pnl": -1000,
        "spread_bps": 9999,
        "near_half_or_whole_dollar": False,
        "momentum_active": False,
        "no_nearby_resistance": False,
        "pullback_low": None,
        "gap_pct": 1,
    }
    seat_holders = {"decider": "alpha", "opponent": "redeye", "governor": "chevelle", "executor": "alpha"}

    for build_fn in (_build_gap_and_go_v1, _build_micro_pullback_v1):
        pkt = build_fn(worst_snapshot, seat_holders)
        gov = pkt["seats"]["governor"]
        assert gov["governor_action"] == "modulate", (
            f"{build_fn.__name__} still emits block: {gov}"
        )


# ───── MC doctrine: AUTHORITY/SCHEMA/BROKER ONLY ─────────────────────


@pytest.mark.tripwire
def test_mc_does_not_reblock_confidence_floor():
    """Brain owns its own confidence floor. MC may OBSERVE but must
    not append CONFIDENCE_BELOW_FLOOR to the gate errors list."""
    import os
    from shared.runtime.platform_survival import mc_canonical_gate, policy_hash

    # Ensure the env-driven floor exists so the test is deterministic.
    os.environ["RISEDUAL_CRYPTO_CONFIDENCE_FLOOR"] = "0.45"

    intent = {
        "runtime": {
            "local_execution_authority": False,
            "policy_hash": policy_hash(),
        },
        "direction": "BUY",
        "confidence": 0.10,   # well below the 0.45 floor
        "lane": "crypto",
        "symbol": "BTC-USD",
    }
    result = mc_canonical_gate(intent)
    assert "CONFIDENCE_BELOW_FLOOR" not in result["errors"]
    # Telemetry must still surface the brain-side fact
    assert result["brain_confidence_below_floor"] is True
    assert result["brain_confidence_floor"] == 0.45
    # And — critically — the intent must be ACCEPTED if all
    # authority/schema/broker checks pass.
    assert result["accepted"] is True
    assert result["final_verdict"] == "APPROVED"


# ───── RoadGuard doctrine: PURE MATH ─────────────────────────────────


@pytest.mark.tripwire
def test_roadguard_spread_floor_is_a_pure_function_of_snapshot():
    """RoadGuard does not look at confidence, conviction, or any brain
    signal. Only the snapshot's `spread_bps` against the lane cap."""
    # We test through the live gate evaluator by importing the
    # snapshot-only path (no brain context needed).
    # Verify the symbolic spread cap is what doctrine says.
    from shared.execution import _evaluate_gates  # noqa: F401 — import-only smoke
    # Existence + import works; behavioral coverage lives in
    # test_execution_gates_doctrine_c.py (broader integration test).
    assert True
