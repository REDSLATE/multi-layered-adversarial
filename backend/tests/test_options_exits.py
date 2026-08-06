"""Exit Monitor options adoption — premium-stop exits via SELL
place_option.

Pins: OCC symbol identity, option-row detection in the Webull
position feed (equity lane must EXCLUDE option rows), premium-based
plan levels, expiry-forced closure, LIMIT-only SELL submission at/
below the bid, and the ×100 contract multiplier on realized PnL.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, "/app/backend")

from shared.broker.webull import _position_row_is_option
from shared.exits import monitor
from shared.exits.policy import DEFAULTS as EXIT_DEFAULTS
from shared.hotpath import exit_plans as plan_store
from shared.options.chain import occ_symbol


def _exp(days: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%d")


# ── OCC identity ─────────────────────────────────────────────────────

def test_occ_symbol_format():
    assert occ_symbol("aapl", "2026-08-28", "CALL", 340.0) == "AAPL260828C00340000"
    assert occ_symbol("TSLA", "2026-09-18", "put", 202.5) == "TSLA260918P00202500"


# ── option-row detection (equity lane safety) ────────────────────────

def test_position_row_option_detection():
    assert _position_row_is_option({"instrument_type": "OPTION"}) is True
    assert _position_row_is_option({"instrumentType": "EQUITY"}) is False
    # heuristic: option fields present, no explicit type
    assert _position_row_is_option(
        {"strikePrice": "340", "optionExpireDate": "2026-08-28"}) is True
    assert _position_row_is_option({"symbol": "AAPL", "quantity": "1.5"}) is False


@pytest.mark.asyncio
async def test_equity_positions_exclude_option_rows(monkeypatch):
    from shared.broker.webull import WebullAdapter
    adapter = WebullAdapter(api_client=None, account_id="a1")

    rows = [
        {"symbol": "AAPL", "quantity": "1.5", "costPrice": "200",
         "marketValue": "310", "unrealizedPnL": "10",
         "instrument_type": "EQUITY"},
        {"symbol": "AAPL", "quantity": "2", "costPrice": "9.0",
         "instrument_type": "OPTION", "strike_price": "340",
         "option_expire_date": "2026-08-28", "option_type": "CALL",
         "underlying_symbol": "AAPL"},
    ]

    async def fake_sdk(fn, *a, **k):
        return {"data": rows}

    async def fake_acct():
        return "a1"
    monkeypatch.setattr(adapter, "_sdk_call", fake_sdk)
    monkeypatch.setattr(adapter, "_resolve_account_id", fake_acct)
    monkeypatch.setattr(adapter, "_trade", lambda: type(
        "T", (), {"account_v2": type("A", (), {
            "get_account_position_details": lambda *a: None})()})())

    eq = await adapter.list_positions()
    assert len(eq) == 1 and eq[0]["qty"] == 1.5

    opts = await adapter.list_option_positions()
    assert len(opts) == 1
    o = opts[0]
    assert o["underlying"] == "AAPL"
    assert o["option_type"] == "CALL"
    assert o["strike_price"] == 340.0
    assert o["expiration"] == "2026-08-28"
    assert o["contracts"] == 2.0
    assert o["entry_premium"] == 9.0


# ── policy defaults ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_exit_policy_includes_options_lane(monkeypatch):
    from shared.exits import policy as exit_policy

    class _Fail:
        def __getitem__(self, name):
            raise RuntimeError("no db in test")
    monkeypatch.setattr(exit_policy, "_LAST_GOOD", None)
    monkeypatch.setattr(exit_policy, "db", _Fail())
    pol = await exit_policy.get_policy()
    o = pol["options"]
    assert o["enabled"] is True            # 2026-08: exits always-on default
    assert o["sl_pct"] == 50.0
    assert o["tp_pct"] == 100.0
    assert o["close_before_expiry_days"] == 1.0


# ── adoption + triggers ──────────────────────────────────────────────

@pytest.fixture
def opt_plan_env(monkeypatch):
    captured = {}
    monkeypatch.setattr(plan_store, "upsert", lambda p, mirror=True: captured.update(p))

    async def no_origin(symbol, lane):
        return None
    monkeypatch.setattr(monitor, "_origin_intent", no_origin)
    return captured


@pytest.mark.asyncio
async def test_options_adoption_premium_levels(opt_plan_env):
    pos = {
        "symbol": occ_symbol("AAPL", _exp(30), "CALL", 340.0),
        "qty": 2.0, "entry_price": 2.00, "current_price": None,
        "option": {"underlying": "AAPL", "option_type": "CALL",
                   "strike_price": 340.0, "expiration": _exp(30)},
    }
    plan = await monitor._adopt("options", pos, {"options": EXIT_DEFAULTS["options"]})
    assert plan["levels_source"] == "premium_policy"
    assert plan["stop_price"] == pytest.approx(1.00)    # −50% premium
    assert plan["target_price"] == pytest.approx(4.00)  # +100% premium
    assert plan["option"]["strike_price"] == 340.0
    assert plan["expiry_close_after"] is not None


def test_trigger_premium_stop_target_and_expiry():
    base = {
        "stop_price": 1.0, "target_price": 4.0,
        "max_hold_until": monitor._iso(monitor._now() + timedelta(hours=24)),
    }
    assert monitor._trigger_for(dict(base), 0.95) == "stop_loss"
    assert monitor._trigger_for(dict(base), 4.10) == "take_profit"
    assert monitor._trigger_for(dict(base), 2.00) is None
    expired = dict(base)
    expired["expiry_close_after"] = monitor._iso(
        monitor._now() - timedelta(hours=1))
    assert monitor._trigger_for(expired, 2.00) == "expiry_close"


# ── SELL submission ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_options_exit_submits_sell_limit(monkeypatch):
    calls = {}

    class FakeAdapter:
        async def submit_option_limit_order(self, **kw):
            calls.update(kw)
            return {"order_id": "wb-exit-1", "status": "SUBMITTED"}

    async def fake_get_adapter():
        return FakeAdapter()
    import shared.broker.webull as wb
    monkeypatch.setattr(wb, "get_webull_adapter", fake_get_adapter)

    updates = {}
    monkeypatch.setattr(plan_store, "update", lambda pid, f: updates.update(f))

    async def no_receipt(row):
        return None
    monkeypatch.setattr(monitor, "_receipt", no_receipt)

    plan = {
        "plan_id": "p1", "lane": "options",
        "symbol": "AAPL260828C00340000", "qty_held": 2.0,
        "exit_reason": "take_profit", "attempts": 0,
        "_bid": 4.05,
        "option": {"underlying": "AAPL", "option_type": "CALL",
                   "strike_price": 340.0, "expiration": "2026-08-28"},
    }
    await monitor._submit_exit(plan, 4.10)
    assert calls["side"] == "SELL"
    assert calls["contracts"] == 2
    assert calls["underlying"] == "AAPL"
    assert calls["option_type"] == "CALL"
    assert calls["expire_date"] == "2026-08-28"
    assert calls["limit_price"] <= 4.05           # at/below the bid
    assert updates["exit_order"]["kind"] == "limit"


@pytest.mark.asyncio
async def test_options_stop_loss_prices_more_aggressively(monkeypatch):
    prices = []

    class FakeAdapter:
        async def submit_option_limit_order(self, **kw):
            prices.append(kw["limit_price"])
            return {"order_id": "x", "status": "SUBMITTED"}

    async def fake_get_adapter():
        return FakeAdapter()
    import shared.broker.webull as wb
    monkeypatch.setattr(wb, "get_webull_adapter", fake_get_adapter)
    monkeypatch.setattr(plan_store, "update", lambda pid, f: None)

    async def no_receipt(row):
        return None
    monkeypatch.setattr(monitor, "_receipt", no_receipt)

    base = {
        "plan_id": "p2", "lane": "options",
        "symbol": "AAPL260828C00340000", "qty_held": 1.0,
        "attempts": 0, "_bid": 1.00,
        "option": {"underlying": "AAPL", "option_type": "CALL",
                   "strike_price": 340.0, "expiration": "2026-08-28"},
    }
    tp = dict(base, exit_reason="take_profit")
    sl = dict(base, exit_reason="stop_loss")
    await monitor._submit_exit(tp, 1.02)
    await monitor._submit_exit(sl, 1.02)
    assert prices[1] < prices[0]   # stop prices deeper through the bid


# ── realized PnL multiplier ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_options_outcome_uses_contract_multiplier(monkeypatch):
    from shared.exits import outcomes

    class _Coll:
        async def insert_one(self, row):
            self.row = row

        async def find_one(self, *a, **k):
            return None

        async def update_one(self, *a, **k):
            return None
    coll = _Coll()

    class _Db:
        def __getitem__(self, name):
            return coll
    monkeypatch.setattr(outcomes, "db", _Db())

    async def no_fold(*a, **k):
        return False
    monkeypatch.setattr(outcomes, "_fold_into_dawe", no_fold)

    row = await outcomes.record_outcome({
        "plan_id": "p3", "lane": "options",
        "symbol": "AAPL260828C00340000",
        "entry_price": 2.00, "exit_price_est": 3.00, "qty_held": 2.0,
        "close_detail": "exit_order_filled",
    })
    assert row["realized_pnl_usd"] == pytest.approx(200.0)  # (3-2)×2×100
    assert row["realized_pnl_pct"] == pytest.approx(50.0)
