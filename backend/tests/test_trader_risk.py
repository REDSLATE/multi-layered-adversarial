"""Tests for /app/trader/risk.py — Mongo-free pre-trade limits.

Proves the risk gate resolves every decision from the local store
+ in-memory state, with no Mongo interaction whatsoever.
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, "/app")

from trader import risk, state, store  # noqa: E402


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@pytest.fixture()
def fresh(tmp_path, monkeypatch):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    store.init(str(tmp_path / "s.sqlite"), str(tmp_path / "j"))
    monkeypatch.setenv("TRADER_PER_ORDER_USD_CAP", "10")
    monkeypatch.setenv("TRADER_DAILY_USD_CAP", "50")
    # Explicitly arm the master switch + enable lanes.
    import trader.state as _s
    _s._master_armed = True
    _s._lane_enabled.clear()
    _s._lane_enabled.update({"equity": True, "crypto": True})
    yield loop
    store.close()
    loop.close()
    asyncio.set_event_loop(None)


def test_master_switch_disarmed_blocks(fresh):
    import trader.state as _s
    _s._master_armed = False
    v = fresh.run_until_complete(
        risk.check(None, {"intent_id": "x", "lane": "equity"})
    )
    assert v.ok is False
    assert v.reason == "master_switch_disarmed"


def test_lane_disabled_blocks(fresh):
    state._lane_enabled["equity"] = False
    v = fresh.run_until_complete(
        risk.check(None, {"intent_id": "x", "lane": "equity"})
    )
    assert v.ok is False
    assert "lane_disabled" in v.reason


def test_per_order_cap_clamps_notional(fresh):
    v = fresh.run_until_complete(
        risk.check(None, {"intent_id": "x", "lane": "equity"},
                   notional_usd=999.0)
    )
    assert v.ok is True
    assert v.notional_usd == 10.0   # clamped to per_order cap


def test_daily_cap_blocks_after_spent(fresh):
    # Record $48 of fills today; per_order cap is $10, daily is $50.
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for i in range(4):
        store.record_execution({
            "intent_id": f"prior-{i}", "ts": f"{today}T12:{i:02d}:00+00:00",
            "notional_usd": 12.0, "ok": True,
        })
    # spent=48; a new $10 order → 48+10=58 > 50 → blocked.
    v = fresh.run_until_complete(
        risk.check(None, {"intent_id": "new", "lane": "equity"},
                   notional_usd=10.0)
    )
    assert v.ok is False
    assert "daily_cap_exceeded" in v.reason
    assert v.spent_today_usd == pytest.approx(48.0)


def test_idempotency_blocks_repeat_fill(fresh):
    intent_id = "rerun-me"
    store.record_execution({
        "intent_id": intent_id, "ts": _iso(),
        "notional_usd": 5.0, "ok": True,
    })
    v = fresh.run_until_complete(
        risk.check(None, {"intent_id": intent_id, "lane": "equity"})
    )
    assert v.ok is False
    assert v.reason == "already_executed"


def test_happy_path_returns_ok(fresh):
    v = fresh.run_until_complete(
        risk.check(None, {"intent_id": "happy", "lane": "equity"})
    )
    assert v.ok is True
    assert v.reason == "ok"
    assert v.notional_usd == 10.0
    assert v.spent_today_usd == 0.0


def test_risk_check_never_touches_mongo(fresh):
    """The `db` argument is accepted but must be ignored. Pass a
    sentinel that would explode if touched, to prove it."""
    class ExplodingDB:
        def __getitem__(self, item):
            raise AssertionError("risk touched Mongo")
        def __getattr__(self, item):
            raise AssertionError("risk touched Mongo")
    v = fresh.run_until_complete(
        risk.check(ExplodingDB(), {"intent_id": "no-mongo", "lane": "equity"})
    )
    assert v.ok is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
