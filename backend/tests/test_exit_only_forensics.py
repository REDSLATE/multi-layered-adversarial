"""Exit-only mode + forensics + promotion gate tests (2026-08-05)."""
from __future__ import annotations

import sys

import pytest

sys.path.insert(0, "/app/backend")

from shared.execution_mode import DEFAULT_MODE, DEFAULTS, hypo_fill_price  # noqa: E402
from shared.forensics.promotion_gate import (  # noqa: E402
    DEFAULTS as GATE_DEFAULTS, counterfactual_return_pct, evaluate_lane,
)
from shared.forensics.trade_forensics import classify_trade, excursions  # noqa: E402

pytestmark = pytest.mark.tripwire


# ── entry-mode governor ─────────────────────────────────────────────

def test_default_mode_is_exit_only():
    # 2026-08-05 operator directive lock — a missing config row must
    # NEVER re-enable live entries
    assert DEFAULT_MODE == "exit_only"
    assert DEFAULTS["mode"] == "exit_only"


@pytest.mark.asyncio
async def test_exit_only_blocks_buy_records_shadow(monkeypatch):
    import shared.execution_mode as em
    async def fake_cfg():
        return {**em.DEFAULTS, "mode": "exit_only"}
    shadows = []
    async def fake_shadow(intent, notional, why):
        shadows.append((intent.get("symbol"), notional, why))
    monkeypatch.setattr(em, "get_entry_mode_config", fake_cfg)
    monkeypatch.setattr(em, "record_shadow_fill", fake_shadow)
    ok, why = await em.gate_new_entry({"symbol": "BTC/USD",
                                       "action": "BUY"}, 5.0)
    assert not ok and "exit_only_mode" in why
    assert shadows and shadows[0][0] == "BTC/USD"


@pytest.mark.asyncio
async def test_live_allows_and_canary_caps(monkeypatch):
    import shared.execution_mode as em
    async def live_cfg():
        return {**em.DEFAULTS, "mode": "live"}
    monkeypatch.setattr(em, "get_entry_mode_config", live_cfg)
    ok, _ = await em.gate_new_entry({"action": "BUY"}, 5.0)
    assert ok
    async def canary_cfg():
        return {**em.DEFAULTS, "mode": "canary",
                "canary_max_trades_per_day": 3}
    monkeypatch.setattr(em, "get_entry_mode_config", canary_cfg)
    async def two_today():
        return 2
    async def noop_shadow(*a):
        pass
    monkeypatch.setattr(em, "_canary_trades_today", two_today)
    monkeypatch.setattr(em, "record_shadow_fill", noop_shadow)
    ok, why = await em.gate_new_entry({"action": "BUY"}, 5.0)
    assert ok and "3/3" in why
    async def three_today():
        return 3
    monkeypatch.setattr(em, "_canary_trades_today", three_today)
    ok2, why2 = await em.gate_new_entry({"action": "BUY"}, 5.0)
    assert not ok2 and "canary daily cap" in why2


def test_sells_never_gated_in_router():
    src = open("/app/backend/shared/broker_router.py").read()
    assert 'in ("BUY", "SHORT")' in src  # gate scoped to entries only
    assert "gate_new_entry" in src


def test_hypo_fill_price_ladder():
    assert hypo_fill_price({"entry_timing_receipt":
                            {"confirmation_price": 2.5}}) == 2.5
    assert hypo_fill_price({"snapshot": {"bid": 1.0, "ask": 1.2}}) == pytest.approx(1.1)
    assert hypo_fill_price({"price_at_signal": 9}) == 9.0


# ── forensic classification (pure) ──────────────────────────────────

def _t(pnl, mfe=None, tp=5.0):
    return {"realized_pnl_pct": pnl, "mfe_pct": mfe, "tp_pct": tp,
            "est_cost_pct": 0.30}


def test_forensic_buckets():
    assert classify_trade(_t(2.0)) == "winner"
    assert classify_trade(_t(-3.0, mfe=6.0)) == "exit_policy"     # saw TP, gave it back
    assert classify_trade(_t(-3.0, mfe=2.0)) == "late_entry"      # right direction, late
    assert classify_trade(_t(-0.35, mfe=0.1)) == "execution_cost"  # loss ≈ costs
    assert classify_trade(_t(-3.0, mfe=0.2)) == "bad_selection"   # straight down
    assert classify_trade(_t(-3.0, mfe=None)) == "bad_selection"
    assert classify_trade({"realized_pnl_pct": None}) == "unknown"


def test_excursions():
    bars = [{"h": 105, "l": 98, "c": 100}, {"h": 108, "l": 101, "c": 103}]
    mfe, mae = excursions(100.0, bars)
    assert mfe == 8.0 and mae == -2.0
    assert excursions(0, bars) == (None, None)


# ── promotion gate (pure) ───────────────────────────────────────────

def test_counterfactual_returns_after_costs():
    assert counterfactual_return_pct(
        {"outcome": "tp_hit", "tp_pct": 5.0}, 0.30) == 4.70
    assert counterfactual_return_pct(
        {"outcome": "sl_hit", "sl_pct": 3.0}, 0.30) == -3.30
    assert counterfactual_return_pct(
        {"outcome": "expired", "end_pct": 1.2}, 0.30) == 0.90
    assert counterfactual_return_pct({"outcome": "no_data"}, 0.30) is None


def test_gate_requires_all_criteria():
    cfg = {**GATE_DEFAULTS, "min_n": 5}
    good = [4.7, -3.3, 4.7, 1.0, 4.7, -3.3, 2.0]
    v = evaluate_lane(good, cfg)
    assert v["passed"] and v["n"] == 7
    # too few observations
    assert not evaluate_lane(good[:3], cfg)["passed"]
    # negative expectancy fails
    bad = [-3.3] * 6 + [4.7]
    v2 = evaluate_lane(bad, cfg)
    assert not v2["passed"]
    assert not [c for c in v2["criteria"]
                if c["name"] == "expectancy_pct_after_costs"][0]["pass"]
    # one giant win carrying everything → single-trade dependence fails
    lucky = [50.0, -1.0, -1.0, -1.0, 0.5, -1.0]
    v3 = evaluate_lane(lucky, cfg)
    dep = [c for c in v3["criteria"]
           if c["name"] == "single_trade_dependence"][0]
    assert not dep["pass"]


def test_wiring():
    reg = open("/app/backend/server_modules/router_registry.py").read()
    assert "routes.execution_mode_admin:router" in reg
    assert "routes.forensics_admin:router" in reg
    me = open("/app/backend/shared/risk_sizer/missed_entries.py").read()
    assert "exit_only_mode" in me  # shadow entries feed the ledger
