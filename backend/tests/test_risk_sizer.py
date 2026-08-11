"""Dynamic risk sizer — 2026-07-25 operator spec.

Pins the operator's exact examples ($4,500 equity → $22.50 risk →
$1,125 at 2% stop / $750 at 3%), reduce-only Governor, portfolio
open-risk capacity, canonical stop bounds + fallback, balance
fail-closed rules, pending reservations, primary-brain selection
weighting, and THE key invariant: recomputed projected loss from the
persisted entry/stop/quantity never exceeds the approved risk budget.
"""
from __future__ import annotations

import sys

import pytest

sys.path.insert(0, "/app/backend")

from shared.risk_sizer import balance, open_risk, selection
from shared.risk_sizer import policy as sizer_policy
from shared.risk_sizer.sizer import build_position_plan, resolve_canonical_stop

POLICY = {
    "crypto": dict(sizer_policy.DEFAULTS["crypto"]),
    "equity": dict(sizer_policy.DEFAULTS["equity"]),
    "selection": dict(sizer_policy.DEFAULTS["selection"]),
    "balance": dict(sizer_policy.DEFAULTS["balance"]),
    "enabled": {"crypto": True, "equity": False},
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
                "equity": {"enabled": False, "sl_pct": 3.0, "tp_pct": 6.0}}

    monkeypatch.setattr("shared.risk_sizer.policy.get_sizer_policy", fake_policy)
    monkeypatch.setattr("shared.risk_sizer.balance.get_balance_snapshot", fake_snapshot)
    monkeypatch.setattr("shared.exits.policy.get_policy", fake_exit_policy)
    monkeypatch.setattr("shared.risk_sizer.open_risk.open_plan_risk", lambda lane: 0.0)
    monkeypatch.setattr(
        "shared.hotpath.policy_snapshot.get",
        lambda: {"master_switch_enabled": True, "broker_freeze_reason": None},
    )

    # pure sizing-math tests: neutralize the BUY eligibility gate
    # (it has its own suite; the $5/trade cap would mask the math here)
    async def fake_elig(sym):
        return True, {"notional_cap_usd": None, "reason": "test_bypass"}
    monkeypatch.setattr(
        "shared.risk_sizer.buy_eligibility.evaluate_buy_eligibility", fake_elig)

    # pure sizing-math tests: neutralize the ATR volatility stop so
    # policy-fallback math stays deterministic (ATR has its own suite)
    async def fake_atr(sym, entry):
        return None
    monkeypatch.setattr("shared.risk_sizer.sizer._atr_fraction", fake_atr)

    async def fake_cooldown(mins):
        return 0.0, None
    monkeypatch.setattr(
        "shared.risk_sizer.sell_cooldown.cooldown_remaining_s", fake_cooldown)
    yield monkeypatch
    balance.reset_for_tests()
    open_risk.reset_for_tests()


def _intent(**over):
    doc = {"intent_id": "rs-test-1", "lane": "crypto", "symbol": "BTC/USD",
           "action": "BUY", "price_at_signal": 118000.0}
    doc.update(over)
    return doc


# ───────────────────── canonical stop resolution ────────────────────

@pytest.mark.asyncio
async def test_valid_brain_stop_wins(wired):
    stop = await resolve_canonical_stop(
        _intent(stop_price=115640.0), POLICY["crypto"])
    assert stop["source"] == "BRAIN"
    assert stop["stop_fraction"] == pytest.approx(0.02)


@pytest.mark.asyncio
async def test_too_tight_brain_stop_falls_back_to_policy(wired):
    stop = await resolve_canonical_stop(
        _intent(stop_price=117800.0), POLICY["crypto"])  # 0.17% — below 1% bound
    assert stop["source"] == "EXIT_POLICY"
    assert stop["stop_fraction"] == pytest.approx(0.03)
    assert "rejected" in (stop["rejected_brain_stop"] or "")


@pytest.mark.asyncio
async def test_wrong_side_brain_stop_falls_back(wired):
    stop = await resolve_canonical_stop(
        _intent(stop_price=120000.0), POLICY["crypto"])  # stop above BUY entry
    assert stop["source"] == "EXIT_POLICY"


# ───────────────────────── sizing math ──────────────────────────────

@pytest.mark.asyncio
async def test_operator_example_2pct_stop_1125(wired):
    plan = await build_position_plan(
        _intent(stop_price=115640.0), governor_multiplier=1.0)
    assert plan["approved"] is True
    assert plan["risk_budget_max"] == pytest.approx(22.50)
    assert plan["final_notional"] == pytest.approx(1125.0)
    assert plan["stop_source"] == "BRAIN"
    assert plan["allocation_cap"] == pytest.approx(1125.0)


@pytest.mark.asyncio
async def test_policy_fallback_3pct_stop_750(wired):
    plan = await build_position_plan(_intent(), governor_multiplier=1.0)
    assert plan["approved"] is True
    assert plan["stop_source"] == "EXIT_POLICY"
    assert plan["final_notional"] == pytest.approx(750.0)


@pytest.mark.asyncio
async def test_governor_half_is_reduce_only(wired):
    plan = await build_position_plan(
        _intent(stop_price=115640.0), governor_multiplier=0.5)
    assert plan["final_notional"] == pytest.approx(562.50)
    # governor reduces BOTH the risk budget and the notional
    assert plan["risk_budget"] == pytest.approx(11.25)
    # multiplier >1 is clamped — governor can never ADD risk
    plan2 = await build_position_plan(
        _intent(intent_id="rs-test-2", stop_price=115640.0),
        governor_multiplier=2.0)
    assert plan2["final_notional"] <= 1125.0
    assert plan2["governor_multiplier"] == 1.0


@pytest.mark.asyncio
async def test_portfolio_risk_capacity_caps_size(wired):
    # 2% of $4,500 = $90 max open risk; $80 already deployed → $10 left
    wired.setattr("shared.risk_sizer.open_risk.open_plan_risk", lambda lane: 80.0)
    plan = await build_position_plan(
        _intent(stop_price=115640.0), governor_multiplier=1.0)
    assert plan["approved"] is True
    assert plan["risk_budget_max"] == pytest.approx(10.0)
    assert plan["final_notional"] == pytest.approx(500.0)
    # exhausted → hard reject
    wired.setattr("shared.risk_sizer.open_risk.open_plan_risk", lambda lane: 95.0)
    plan2 = await build_position_plan(
        _intent(intent_id="rs-test-3", stop_price=115640.0),
        governor_multiplier=1.0)
    assert plan2["approved"] is False
    assert plan2["reason"] == "portfolio_risk_budget_exhausted"


@pytest.mark.asyncio
async def test_pending_reservations_prevent_double_sizing(wired):
    """Two entries sized in the same pulse must not each consume the
    full remaining risk budget."""
    p1 = await build_position_plan(
        _intent(intent_id="rs-a", stop_price=115640.0), governor_multiplier=1.0)
    assert p1["risk_budget"] == pytest.approx(22.50)
    p2 = await build_position_plan(
        _intent(intent_id="rs-b", stop_price=115640.0), governor_multiplier=1.0)
    assert p2["portfolio_open_risk"] == pytest.approx(22.50)
    p3 = await build_position_plan(
        _intent(intent_id="rs-c", stop_price=115640.0), governor_multiplier=1.0)
    p4 = await build_position_plan(
        _intent(intent_id="rs-d", stop_price=115640.0), governor_multiplier=1.0)
    # 4th trade: 3 × $22.50 = $67.50 reserved of $90 → only $22.50 left
    assert p4["approved"] is True
    total_reserved = sum(p["risk_budget"] for p in (p1, p2, p3, p4))
    assert total_reserved <= 90.0 + 1e-6


@pytest.mark.asyncio
async def test_balance_fail_closed(wired):
    async def none_snapshot(lane, **_kw):
        return None
    wired.setattr("shared.risk_sizer.balance.get_balance_snapshot", none_snapshot)
    plan = await build_position_plan(
        _intent(stop_price=115640.0), governor_multiplier=1.0)
    assert plan["approved"] is False
    assert plan["reason"] == "no_balance_no_trade"


@pytest.mark.asyncio
async def test_key_invariant_projected_loss_within_budget(wired):
    """THE acceptance test: recompute projected loss from persisted
    entry, stop, and quantity — it must never exceed the approved
    risk budget."""
    for gov in (1.0, 0.5, 0.25):
        for stop_price in (115640.0, 116820.0, None):  # 2%, 1%, policy 3%
            intent = _intent(intent_id=f"rs-inv-{gov}-{stop_price}")
            if stop_price:
                intent["stop_price"] = stop_price
            plan = await build_position_plan(intent, governor_multiplier=gov)
            open_risk.reset_for_tests()
            if not plan["approved"]:
                continue
            entry = plan["entry_price"]
            stop = plan["stop_price"] or entry * (1 - plan["stop_distance"])
            qty = plan["final_notional"] / entry
            projected_loss = max(0.0, entry - stop) * qty
            assert projected_loss <= plan["risk_budget_max"] + 0.01, (
                f"gov={gov} stop={stop_price}: loss {projected_loss} "
                f"> budget {plan['risk_budget_max']}"
            )


@pytest.mark.asyncio
async def test_edge_gate_rejects_when_costs_exceed_edge(wired):
    """Edge-vs-cost check (cleaned convex core): 0.4% expected edge
    < 0.6% fee buffer → reject; 2% edge → pass; no edge data → skip."""
    plan = await build_position_plan(
        _intent(intent_id="rs-edge-1", stop_price=115640.0,
                expected_edge_fraction=0.004),
        governor_multiplier=1.0)
    assert plan["approved"] is False
    assert plan["reason"] == "edge_does_not_cover_costs"
    plan2 = await build_position_plan(
        _intent(intent_id="rs-edge-2", stop_price=115640.0,
                expected_edge_fraction=0.02),
        governor_multiplier=1.0)
    assert plan2["approved"] is True
    plan3 = await build_position_plan(
        _intent(intent_id="rs-edge-3", stop_price=115640.0),
        governor_multiplier=1.0)
    assert plan3["approved"] is True  # no edge data → gate skipped


@pytest.mark.asyncio
async def test_receipt_carries_plan_id_and_projected_loss(wired):
    plan = await build_position_plan(
        _intent(intent_id="rs-pid", stop_price=115640.0),
        governor_multiplier=1.0)
    assert plan["approved"] is True
    assert len(plan["plan_id"]) == 32
    assert plan["projected_loss_at_stop"] <= plan["risk_budget_max"] + 0.01


# ─────────────────── primary-brain selection ────────────────────────

def _cand(brain, conf, **over):
    c = {"brain": brain, "direction": "long", "rank_score": conf,
         "effective_weight": 1.0, "adjusted_rank": conf,
         "opinion": {"confidence": conf}}
    c.update(over)
    return c


@pytest.mark.asyncio
async def test_selection_renormalized_below_sample(wired):
    async def fake_exp(lane):
        return {"camino": {"trades": 5, "expectancy_usd": -0.5}}
    wired.setattr("shared.risk_sizer.selection._brain_expectancy", fake_exp)

    async def fake_kernel(brain, lane):
        return {"score": 0.6}
    wired.setattr("shared.brains.kernel_throttle.get_kernel_throttle", fake_kernel)

    cands = [_cand("camino", 0.72), _cand("gto", 0.40)]
    await selection.annotate_candidates(cands, "crypto")
    camino, gto = cands
    # below-sample negative expectancy must NOT block (soft gate)
    assert camino["eligible"] is True
    assert camino["selection_detail"]["expectancy_weighted"] is False
    # renormalized: 0.72×(0.35/0.85) + 0.5×(0.30/0.85) + 0.6×(0.20/0.85)
    expected = 0.72 * 0.35 / 0.85 + 0.5 * 0.30 / 0.85 + 0.6 * 0.20 / 0.85
    assert camino["selection_score"] == pytest.approx(expected, abs=1e-3)
    # confidence floor: 0.40 < 0.55
    assert gto["eligible"] is False
    assert "confidence" in gto["ineligible_reason"]


@pytest.mark.asyncio
async def test_selection_hard_gate_at_sample(wired):
    async def fake_exp(lane):
        return {"camino": {"trades": 25, "expectancy_usd": -0.10},
                "gto": {"trades": 25, "expectancy_usd": 0.40}}
    wired.setattr("shared.risk_sizer.selection._brain_expectancy", fake_exp)

    async def fake_kernel(brain, lane):
        return {"score": 0.6}
    wired.setattr("shared.brains.kernel_throttle.get_kernel_throttle", fake_kernel)

    cands = [_cand("camino", 0.80), _cand("gto", 0.70)]
    await selection.annotate_candidates(cands, "crypto")
    camino, gto = cands
    assert camino["eligible"] is False
    assert camino["ineligible_reason"] == "negative_verified_expectancy"
    assert gto["eligible"] is True
    assert gto["selection_detail"]["expectancy_weighted"] is True
