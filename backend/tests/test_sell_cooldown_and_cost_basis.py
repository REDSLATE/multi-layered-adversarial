"""Post-sell cooldown + true-cost-basis fixes (2026-07-28).

Operator report: (1) unprofitable crypto never sold — Kraken Balance
has no cost basis, so exit levels anchored to adoption-time price;
(2) the moment a sell freed cash, a fresh mover-chasing BUY intent
redeployed it within seconds.
"""
from __future__ import annotations

import sys

import pytest

sys.path.insert(0, "/app/backend")

from shared.exits.monitor import _vwap_cost_basis
from shared.risk_sizer import balance, open_risk, selection
from shared.risk_sizer import policy as sizer_policy
from shared.risk_sizer import sell_cooldown
from shared.risk_sizer.sizer import build_position_plan

POLICY = {
    "crypto": dict(sizer_policy.DEFAULTS["crypto"]),
    "equity": dict(sizer_policy.DEFAULTS["equity"]),
    "options": dict(sizer_policy.DEFAULTS["options"]),
    "selection": dict(sizer_policy.DEFAULTS["selection"]),
    "balance": dict(sizer_policy.DEFAULTS["balance"]),
    "enabled": {"crypto": True, "equity": True, "options": True},
}


# ── _vwap_cost_basis ────────────────────────────────────────────────

def _trade(pair, ttype, price, vol, t):
    return {"pair": pair, "type": ttype, "price": price, "vol": vol, "time": t}


def test_vwap_single_buy_covers_qty():
    trades = {"a": _trade("XETHZUSD", "buy", 2000.0, 1.0, 100)}
    assert _vwap_cost_basis(trades, "ETH/USD", 1.0) == 2000.0


def test_vwap_weighted_across_recent_buys():
    trades = {
        "a": _trade("XETHZUSD", "buy", 1000.0, 0.5, 200),  # most recent
        "b": _trade("XETHZUSD", "buy", 2000.0, 0.5, 100),
    }
    assert _vwap_cost_basis(trades, "ETH/USD", 1.0) == 1500.0


def test_vwap_ignores_sells_and_other_pairs():
    trades = {
        "a": _trade("XETHZUSD", "sell", 999.0, 5.0, 300),
        "b": _trade("SOLUSD", "buy", 150.0, 10.0, 250),
        "c": _trade("XETHZUSD", "buy", 2500.0, 1.0, 100),
    }
    assert _vwap_cost_basis(trades, "ETH/USD", 1.0) == 2500.0


def test_vwap_btc_xbt_alias():
    trades = {"a": _trade("XXBTZUSD", "buy", 60000.0, 0.01, 100)}
    assert _vwap_cost_basis(trades, "BTC/USD", 0.01) == 60000.0


def test_vwap_partial_take_of_oldest_buy():
    # holds 1.0; recent buy 0.6 @ 100, older buy 1.0 @ 200 → take 0.4
    trades = {
        "a": _trade("SOLUSD", "buy", 100.0, 0.6, 200),
        "b": _trade("SOLUSD", "buy", 200.0, 1.0, 100),
    }
    assert _vwap_cost_basis(trades, "SOL/USD", 1.0) == pytest.approx(140.0)


def test_vwap_insufficient_coverage_returns_none():
    trades = {"a": _trade("XETHZUSD", "buy", 2000.0, 0.4, 100)}
    assert _vwap_cost_basis(trades, "ETH/USD", 1.0) is None


def test_vwap_no_trades_returns_none():
    assert _vwap_cost_basis({}, "ETH/USD", 1.0) is None


# ── sell_cooldown state machine ─────────────────────────────────────

async def test_cooldown_clear_before_any_sell():
    sell_cooldown.reset_for_tests()
    rem, sym = await sell_cooldown.cooldown_remaining_s(30.0)
    assert rem == 0.0 and sym is None


async def test_cooldown_arms_on_sell_and_disabled_at_zero():
    sell_cooldown.reset_for_tests()
    sell_cooldown.note_crypto_sell("ETH/USD")
    rem, sym = await sell_cooldown.cooldown_remaining_s(30.0)
    assert rem > 0 and sym == "ETH/USD"
    rem0, _ = await sell_cooldown.cooldown_remaining_s(0.0)
    assert rem0 == 0.0


async def test_cooldown_expires():
    sell_cooldown.reset_for_tests()
    sell_cooldown.note_crypto_sell("ETH/USD")
    sell_cooldown._state["mono"] -= 31 * 60  # age the sell 31 min
    rem, _ = await sell_cooldown.cooldown_remaining_s(30.0)
    assert rem == 0.0


# ── sizer gate integration ──────────────────────────────────────────

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
    doc = {"intent_id": "cr-test-1", "lane": "crypto", "symbol": "SOL/USD",
           "action": "BUY", "price_at_signal": 150.0}
    doc.update(over)
    return doc


async def test_sizer_rejects_buy_during_cooldown(wired):
    sell_cooldown.note_crypto_sell("ETH/USD")
    plan = await build_position_plan(_crypto_intent(), governor_multiplier=1.0)
    assert plan["approved"] is False
    assert plan["reason"] == "post_sell_cooldown"
    assert plan["last_sell_symbol"] == "ETH/USD"
    assert plan["cooldown_remaining_s"] > 0


async def test_sizer_allows_buy_after_cooldown(wired):
    sell_cooldown.note_crypto_sell("ETH/USD")
    sell_cooldown._state["mono"] -= 31 * 60
    plan = await build_position_plan(_crypto_intent(), governor_multiplier=1.0)
    assert plan.get("reason") != "post_sell_cooldown"


async def test_sizer_never_blocks_sells_during_cooldown(wired):
    sell_cooldown.note_crypto_sell("ETH/USD")
    plan = await build_position_plan(
        _crypto_intent(action="SELL"), governor_multiplier=1.0,
    )
    assert plan.get("reason") != "post_sell_cooldown"
