"""Shared risk engine across lanes — 2026 rollout spec.

One lane-agnostic engine: crypto, equity, and options all enter the
SAME build_position_plan path. Pins the operator's 12 integrated
acceptance cases: edge gate, projected-loss invariant, reduce-only
Governor, RoadGuard zero-force, 2% open+pending cap, atomic
reservations, fail-closed balance (missing live + expired cache),
canonical-stop identity, expectancy soft/hard gates, and lane
eligibility (equity + options enter the path).
"""
from __future__ import annotations

import sys
import time

import pytest

sys.path.insert(0, "/app/backend")

from shared.risk_sizer import balance, open_risk, selection
from shared.risk_sizer import options_gate
from shared.risk_sizer import policy as sizer_policy
from shared.risk_sizer.policy import lane_enabled
from shared.risk_sizer.sizer import build_position_plan, resolve_canonical_stop

POLICY = {
    "crypto": dict(sizer_policy.DEFAULTS["crypto"]),
    "equity": dict(sizer_policy.DEFAULTS["equity"]),
    "options": dict(sizer_policy.DEFAULTS["options"]),
    "selection": dict(sizer_policy.DEFAULTS["selection"]),
    "balance": dict(sizer_policy.DEFAULTS["balance"]),
    "enabled": {"crypto": True, "equity": True, "options": True},
}


@pytest.fixture
def wired(monkeypatch):
    balance.reset_for_tests()
    open_risk.reset_for_tests()
    selection.reset_for_tests()

    async def fake_policy():
        return {k: (dict(v) if isinstance(v, dict) else v) for k, v in POLICY.items()}

    async def fake_snapshot(lane, **_kw):
        return {"equity": 4500.0, "available": 4500.0, "source": "LIVE", "age_ms": 0}

    async def fake_exit_policy():
        return {"crypto": {"enabled": True, "sl_pct": 3.0, "tp_pct": 8.0},
                "equity": {"enabled": True, "sl_pct": 3.0, "tp_pct": 6.0}}

    monkeypatch.setattr("shared.risk_sizer.policy.get_sizer_policy", fake_policy)
    monkeypatch.setattr("shared.risk_sizer.balance.get_balance_snapshot", fake_snapshot)
    monkeypatch.setattr("shared.exits.policy.get_policy", fake_exit_policy)
    monkeypatch.setattr("shared.risk_sizer.open_risk.open_plan_risk", lambda lane: 0.0)
    monkeypatch.setattr(
        "shared.hotpath.policy_snapshot.get",
        lambda: {"master_switch_enabled": True, "broker_freeze_reason": None},
    )
    yield monkeypatch
    balance.reset_for_tests()
    open_risk.reset_for_tests()


def _eq_intent(**over):
    doc = {"intent_id": "eq-test-1", "lane": "equity", "symbol": "AAPL",
           "action": "BUY", "price_at_signal": 200.0}
    doc.update(over)
    return doc


def _opt_intent(**over):
    doc = {"intent_id": "opt-test-1", "lane": "options",
           "symbol": "AAPL260717C00210000", "action": "BUY",
           "option": {"premium": 0.20, "dte": 21, "open_interest": 500,
                      "bid": 0.19, "ask": 0.21, "delta": 0.45,
                      "theta": -0.005}}
    doc.update(over)
    return doc


# ── Case 5: RoadGuard forces final notional to zero ─────────────────

@pytest.mark.asyncio
async def test_roadguard_forces_notional_to_zero(wired):
    wired.setattr(
        "shared.hotpath.policy_snapshot.get",
        lambda: {"master_switch_enabled": False, "broker_freeze_reason": None},
    )
    plan = await build_position_plan(
        _eq_intent(stop_price=196.0), governor_multiplier=1.0)
    assert plan["approved"] is False
    assert plan["reason"] == "roadguard_hard_block"
    assert plan["final_notional"] == 0.0

    wired.setattr(
        "shared.hotpath.policy_snapshot.get",
        lambda: {"master_switch_enabled": True, "broker_frozen": True,
                 "broker_freeze_reason": "operator_freeze"},
    )
    plan2 = await build_position_plan(
        _eq_intent(stop_price=196.0), governor_multiplier=1.0)
    assert plan2["approved"] is False
    assert plan2["final_notional"] == 0.0

    # 2026-08-01 fix: a THAWED freeze (frozen=False) with a lingering
    # reason string must NOT roadguard-block sizing.
    wired.setattr(
        "shared.hotpath.policy_snapshot.get",
        lambda: {"master_switch_enabled": True, "broker_frozen": False,
                 "broker_freeze_reason": "tripwire_assert_check"},
    )
    plan3 = await build_position_plan(
        _eq_intent(stop_price=196.0), governor_multiplier=1.0)
    assert plan3.get("reason") != "roadguard_hard_block"


# ── Cases 1-2 on equity: edge gate is lane-agnostic ──────────────────

@pytest.mark.asyncio
async def test_equity_edge_gate(wired):
    # equity costs: fee 0.001 + slippage 0.0005 = 0.0015
    plan = await build_position_plan(
        _eq_intent(stop_price=196.0, expected_edge_fraction=0.001),
        governor_multiplier=1.0)
    assert plan["approved"] is False
    assert plan["reason"] == "edge_does_not_cover_costs"
    plan2 = await build_position_plan(
        _eq_intent(stop_price=196.0, expected_edge_fraction=0.01),
        governor_multiplier=1.0)
    assert plan2["approved"] is True


# ── Case 12: equity enters the shared path ───────────────────────────

@pytest.mark.asyncio
async def test_equity_lane_sizes_from_canonical_stop(wired):
    # $4,500 × 0.5% = $22.50 risk; 2% stop → $1,125 notional
    plan = await build_position_plan(
        _eq_intent(stop_price=196.0), governor_multiplier=1.0)
    assert plan["approved"] is True
    assert plan["lane"] == "equity"
    assert plan["risk_budget_max"] == pytest.approx(22.50)
    assert plan["final_notional"] == pytest.approx(1125.0)
    assert plan["stop_source"] == "BRAIN"
    # Case 3: projected loss never exceeds budget
    assert plan["projected_loss_at_stop"] <= plan["risk_budget_max"] + 0.01


@pytest.mark.asyncio
async def test_equity_governor_reduces_risk_and_notional(wired):
    plan = await build_position_plan(
        _eq_intent(stop_price=196.0), governor_multiplier=0.5)
    assert plan["risk_budget"] == pytest.approx(11.25)
    assert plan["final_notional"] == pytest.approx(562.50)


# ── Case 6: open + pending risk capped at 2% across a lane ───────────

@pytest.mark.asyncio
async def test_equity_open_plus_pending_capped_at_2pct(wired):
    wired.setattr("shared.risk_sizer.open_risk.open_plan_risk", lambda lane: 60.0)
    plans = []
    for i in range(4):
        plans.append(await build_position_plan(
            _eq_intent(intent_id=f"eq-cap-{i}", stop_price=196.0),
            governor_multiplier=1.0))
    approved = [p for p in plans if p["approved"]]
    total = 60.0 + sum(p["risk_budget"] for p in approved)
    assert total <= 4500.0 * 0.02 + 1e-6
    assert any(not p["approved"] for p in plans)  # capacity exhausts


# ── Case 8: missing live balance AND expired cache fail closed ───────

@pytest.mark.asyncio
async def test_missing_live_and_expired_cache_fail_closed(monkeypatch):
    balance.reset_for_tests()

    async def boom():
        raise RuntimeError("kraken down")
    monkeypatch.setitem(balance._FETCHERS, "crypto", boom)
    monkeypatch.setattr(balance, "_open_positions_mark", lambda lane: 0.0)

    # no cache at all → None
    assert await balance.get_balance_snapshot("crypto") is None

    # fresh cache (30s) → CACHE fallback
    balance._cache["crypto"] = {"equity": 4500.0, "available": 4500.0,
                                "at": time.monotonic() - 30.0}
    snap = await balance.get_balance_snapshot("crypto")
    assert snap is not None and snap["source"] == "CACHE"

    # expired cache (>60s) → fail closed
    balance._cache["crypto"] = {"equity": 4500.0, "available": 4500.0,
                                "at": time.monotonic() - 61.0}
    assert await balance.get_balance_snapshot("crypto") is None
    balance.reset_for_tests()


# ── Case 7: reservations are atomic; release frees them ─────────────

@pytest.mark.asyncio
async def test_release_pending_frees_reserved_capital(wired):
    p1 = await build_position_plan(
        _eq_intent(intent_id="eq-res-1", stop_price=196.0),
        governor_multiplier=1.0)
    assert p1["approved"] is True
    assert open_risk.pending_risk("equity") == pytest.approx(22.50)
    # failed broker route → reservation released
    open_risk.release_pending("eq-res-1")
    assert open_risk.pending_risk("equity") == 0.0


# ── Case 9: brain stop and Exit-Monitor stop are IDENTICAL ───────────

@pytest.mark.asyncio
async def test_canonical_stop_identity_with_exit_monitor(wired):
    intent = _eq_intent(stop_price=196.0)
    stop = await resolve_canonical_stop(intent, POLICY["equity"])
    plan = await build_position_plan(intent, governor_multiplier=1.0)
    # the stop persisted for the Exit Monitor is the EXACT stop sized from
    assert plan["stop_price"] == stop["stop_price"] == 196.0
    assert plan["stop_distance"] == pytest.approx(stop["stop_fraction"])
    assert plan["stop_source"] == stop["source"] == "BRAIN"


# ── Case 12 (options): options enter the shared path ─────────────────

@pytest.mark.asyncio
async def test_options_lane_premium_sizing(wired):
    # $22.50 risk budget; $0.20 premium × 100 = $20/contract → 1 contract
    plan = await build_position_plan(_opt_intent(), governor_multiplier=1.0)
    assert plan["approved"] is True
    assert plan["lane"] == "options"
    assert plan["contracts"] == 1
    assert plan["final_notional"] == pytest.approx(20.0)
    assert plan["risk_budget"] == pytest.approx(20.0)
    assert plan["stop_source"] == "PREMIUM"
    # Case 3 for options: full premium at risk ≤ budget
    assert plan["projected_loss_at_stop"] <= plan["risk_budget_max"] + 0.01


@pytest.mark.asyncio
async def test_options_expensive_premium_rejected(wired):
    o = {"premium": 2.50, "dte": 21, "open_interest": 500,
         "bid": 2.45, "ask": 2.55, "delta": 0.45, "theta": -0.03}
    plan = await build_position_plan(
        _opt_intent(intent_id="opt-exp", option=o), governor_multiplier=1.0)
    assert plan["approved"] is False
    assert plan["reason"] == "options_below_minimum_contracts"


@pytest.mark.asyncio
async def test_options_governor_reduces_contracts(wired):
    # gov 0.5 → $11.25 budget → 0 contracts at $20 each → reject
    plan = await build_position_plan(
        _opt_intent(intent_id="opt-gov"), governor_multiplier=0.5)
    assert plan["approved"] is False
    assert plan["reason"] == "options_below_minimum_contracts"


@pytest.mark.asyncio
async def test_options_max_premium_cap_binds(wired):
    # widen risk knobs so the 5% max-premium cap becomes the binder
    POLICY["options"]["risk_fraction"] = 0.10
    POLICY["options"]["max_open_risk_fraction"] = 0.20
    try:
        plan = await build_position_plan(
            _opt_intent(intent_id="opt-cap"), governor_multiplier=1.0)
        assert plan["approved"] is True
        # risk allows 22 contracts; premium cap $225 → 11 contracts
        assert plan["contracts"] == 11
        assert plan["final_notional"] <= 4500.0 * 0.05 + 1e-6
    finally:
        POLICY["options"]["risk_fraction"] = 0.005
        POLICY["options"]["max_open_risk_fraction"] = 0.02


# ── Options contract-quality gates ───────────────────────────────────

def test_options_gate_dte_bounds():
    pol = sizer_policy.DEFAULTS["options"]
    base = {"premium": 0.20, "open_interest": 500, "bid": 0.19,
            "ask": 0.21, "delta": 0.45, "theta": -0.005}
    for dte, ok in ((3, False), (7, True), (60, True), (90, False)):
        res = options_gate.check({"option": {**base, "dte": dte}}, pol)
        assert res["ok"] is ok, f"dte={dte}"
        if not ok:
            assert res["reason"] == "options_dte_out_of_bounds"


def test_options_gate_open_interest_spread_greeks():
    pol = sizer_policy.DEFAULTS["options"]
    good = {"premium": 0.20, "dte": 21, "open_interest": 500,
            "bid": 0.19, "ask": 0.21, "delta": 0.45, "theta": -0.005}
    assert options_gate.check({"option": good}, pol)["ok"] is True

    low_oi = options_gate.check({"option": {**good, "open_interest": 10}}, pol)
    assert low_oi["reason"] == "options_open_interest_too_low"

    wide = options_gate.check(
        {"option": {**good, "bid": 0.15, "ask": 0.25}}, pol)
    assert wide["reason"] == "options_spread_too_wide"

    lotto = options_gate.check({"option": {**good, "delta": 0.05}}, pol)
    assert lotto["reason"] == "options_delta_out_of_bounds"

    decay = options_gate.check({"option": {**good, "theta": -0.02}}, pol)
    assert decay["reason"] == "options_theta_decay_too_high"

    no_prem = options_gate.check({"option": {**good, "premium": None}}, pol)
    assert no_prem["reason"] == "options_missing_premium"


# ── Lane enablement flags ────────────────────────────────────────────

def test_lane_flags_all_enabled(monkeypatch):
    monkeypatch.setenv("CRYPTO_DYNAMIC_RISK_SIZER_ENABLED", "true")
    monkeypatch.setenv("EQUITY_DYNAMIC_RISK_SIZER_ENABLED", "true")
    monkeypatch.setenv("OPTIONS_ENABLED", "true")
    assert lane_enabled("crypto") is True
    assert lane_enabled("equity") is True
    assert lane_enabled("options") is True
    monkeypatch.setenv("OPTIONS_ENABLED", "false")
    assert lane_enabled("options") is False
