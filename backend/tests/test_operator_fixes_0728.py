"""Operator fixes 2026-07-28: SELL inventory gate, broker-min bump,
universe spread filter.
"""
from __future__ import annotations

import sys

import pytest

sys.path.insert(0, "/app/backend")

from shared.risk_sizer import balance, open_risk, selection
from shared.risk_sizer import policy as sizer_policy
from shared.risk_sizer import sell_cooldown
from shared.risk_sizer.sizer import build_position_plan
from shared.universe.refresher import _apply_spread_filter

POLICY = {
    "crypto": dict(sizer_policy.DEFAULTS["crypto"]),
    "equity": dict(sizer_policy.DEFAULTS["equity"]),
    "options": dict(sizer_policy.DEFAULTS["options"]),
    "selection": dict(sizer_policy.DEFAULTS["selection"]),
    "balance": dict(sizer_policy.DEFAULTS["balance"]),
    "enabled": {"crypto": True, "equity": True, "options": True},
}


# ── universe spread filter (fix #4) ─────────────────────────────────

def test_spread_filter_drops_wide_pairs():
    rows = [
        {"canonical_symbol": "BTC/USD", "spread_bps": 2.0},
        {"canonical_symbol": "GAIB/USD", "spread_bps": 180.0},
        {"canonical_symbol": "NEW/USD", "spread_bps": None},
    ]
    kept, dropped = _apply_spread_filter(rows, 60.0, min_keep=2)
    assert [r["canonical_symbol"] for r in kept] == ["BTC/USD", "NEW/USD"]
    assert dropped[0]["canonical_symbol"] == "GAIB/USD"
    assert "wide_spread" in dropped[0]["_drop_reason"]


def test_spread_filter_min_keep_floor_refills_tightest():
    rows = [{"canonical_symbol": f"S{i}/USD", "spread_bps": 100.0 + i}
            for i in range(20)]
    kept, dropped = _apply_spread_filter(rows, 60.0, min_keep=12)
    assert len(kept) == 12
    # refilled with the TIGHTEST spreads first
    assert {r["canonical_symbol"] for r in kept} == {f"S{i}/USD" for i in range(12)}
    assert all("_drop_reason" in r for r in dropped)


def test_spread_filter_pinned_exempt():
    rows = [{"canonical_symbol": "X/USD", "spread_bps": 500.0, "pinned": True}]
    kept, dropped = _apply_spread_filter(rows, 60.0)
    assert len(kept) == 1 and not dropped


# ── broker-min bump (fix #3) ────────────────────────────────────────

@pytest.fixture
def wired(monkeypatch):
    balance.reset_for_tests()
    open_risk.reset_for_tests()
    selection.reset_for_tests()
    sell_cooldown.reset_for_tests()

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
    sell_cooldown.reset_for_tests()


def _crypto_intent(**over):
    doc = {"intent_id": "cr-bump-1", "lane": "crypto", "symbol": "SOL/USD",
           "action": "BUY", "price_at_signal": 150.0}
    doc.update(over)
    return doc


async def test_governor_risk_down_bumps_to_broker_min(wired):
    # gm=0.33 → risk $4500*0.005*0.33 = $7.43; /3% stop = $247 — fine.
    # Force sub-min via tiny gm: 0.02 → risk $0.45 → notional $15?? no:
    # $0.45/0.03 = $15 > $5 min. Use gm small enough: 0.005 → $0.11 →
    # $3.75 notional < $5 min → bump to $5.
    plan = await build_position_plan(_crypto_intent(), governor_multiplier=0.005)
    assert plan["approved"] is True
    assert plan["final_notional"] == 5.0
    assert plan["min_notional_bump"] is True


async def test_bump_disabled_knob_rejects(wired):
    POLICY["crypto"]["bump_to_broker_min"] = False
    try:
        plan = await build_position_plan(
            _crypto_intent(), governor_multiplier=0.005,
        )
        assert plan["approved"] is False
        assert plan["reason"] == "below_minimum_order_notional"
    finally:
        POLICY["crypto"]["bump_to_broker_min"] = True


async def test_normal_sizes_not_bumped(wired):
    plan = await build_position_plan(_crypto_intent(), governor_multiplier=1.0)
    assert plan["approved"] is True
    assert plan["min_notional_bump"] is False
    assert plan["final_notional"] > 5.0
