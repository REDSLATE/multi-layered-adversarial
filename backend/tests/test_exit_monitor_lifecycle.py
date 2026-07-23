"""Exit Monitor full-lifecycle integration (mocked broker) —
reconcile → adopt → trigger → reserve → submit → complete."""
from __future__ import annotations

import sys
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, "/app/backend")

from shared.exits import monitor as em

SYM = "LIFEC/USD"


async def _drain_outbox():
    """Receipts/outcomes now commit to the local SQLite outbox first
    (2026-07-23); apply them to Mongo before asserting."""
    from shared.hotpath import outbox
    from shared.hotpath.handlers import register_all
    register_all()
    await outbox.drain_once()


async def _cleanup():
    from db import db
    from shared.exits.outcomes import EXIT_OUTCOMES
    await db[em.EXIT_PLANS].delete_many({"symbol": SYM})
    await db[em.EXIT_RECEIPTS].delete_many({"symbol": SYM})
    await db[EXIT_OUTCOMES].delete_many({"symbol": SYM})


def _policy(sl=3.0, tp=8.0, hold=48.0):
    return {
        "equity": {"enabled": False, "sl_pct": 3.0, "tp_pct": 6.0, "max_hold_h": 24.0},
        "crypto": {"enabled": True, "sl_pct": sl, "tp_pct": tp, "max_hold_h": hold},
        "escalate_after_s": 120.0,
    }


class _FakeKraken:
    def __init__(self):
        self.market_calls = []
        self.limit_calls = []
        self.cancelled = []

    async def submit_market_order(self, symbol, qty=None, side="BUY", **kw):
        self.market_calls.append((symbol, qty, side))
        return {"order_id": f"MKT-{len(self.market_calls)}"}

    async def submit_limit_order(self, symbol, qty, limit_price, side="BUY", **kw):
        self.limit_calls.append((symbol, qty, limit_price, side))
        return {"order_id": f"LIM-{len(self.limit_calls)}"}

    async def cancel_order(self, order_id):
        self.cancelled.append(order_id)


@pytest.mark.asyncio
async def test_full_lifecycle_stop_loss_market_exit():
    from db import db
    await _cleanup()
    fake = _FakeKraken()
    pos = [{"symbol": SYM, "qty": 2.0, "entry_price": None, "current_price": 100.0}]
    try:
        with patch.object(em, "_crypto_positions", new=AsyncMock(return_value=(pos, 0))), \
             patch.object(em, "_crypto_price", new=AsyncMock(return_value=100.0)), \
             patch.object(em, "_brain_levels", new=AsyncMock(return_value=None)), \
             patch.object(em, "_entry_price_fallback", new=AsyncMock(return_value=100.0)), \
             patch.object(em, "_mint_exit_receipt", return_value={"signature": "x", "mc_policy_hash": "y"}):
            with patch("shared.exits.policy.get_policy", new=AsyncMock(return_value=_policy())):
                # Tick 1: adoption — no trigger at entry price.
                await em.run_once()
                plan = await db[em.EXIT_PLANS].find_one({"symbol": SYM})
                assert plan and plan["status"] == "active"
                assert plan["stop_price"] == pytest.approx(97.0)
                assert plan["target_price"] == pytest.approx(108.0)
                assert plan["qty_held"] == 2.0

                # Tick 2: price crashes through the stop → MARKET exit.
                pos[0]["current_price"] = 96.5
                with patch("shared.crypto.broker_adapter.get_kraken_adapter",
                           new=AsyncMock(return_value=fake)):
                    await em.run_once()
                plan = await db[em.EXIT_PLANS].find_one({"symbol": SYM})
                assert plan["status"] == "exiting"
                assert plan["exit_reason"] == "stop_loss"
                assert plan["exit_order"]["kind"] == "market"
                assert fake.market_calls == [(SYM, 2.0, "SELL")]
                assert not fake.limit_calls

                # Tick 3: position gone at broker → plan closed + receipt.
                with patch.object(em, "_crypto_positions",
                                  new=AsyncMock(return_value=([], 0))):
                    await em.run_once()
                plan = await db[em.EXIT_PLANS].find_one({"symbol": SYM})
                assert plan["status"] == "closed"
                assert plan["close_detail"] == "exit_order_filled"
                await _drain_outbox()
                receipts = await db[em.EXIT_RECEIPTS].find(
                    {"symbol": SYM}).to_list(20)
                events = sorted(r["event"] for r in receipts)
                assert events == ["exit_complete", "exit_submit"]
    finally:
        await _cleanup()


@pytest.mark.asyncio
async def test_take_profit_uses_marketable_limit_then_completes():
    from db import db
    await _cleanup()
    fake = _FakeKraken()
    pos = [{"symbol": SYM, "qty": 1.0, "entry_price": None, "current_price": 100.0}]
    try:
        with patch.object(em, "_crypto_positions", new=AsyncMock(return_value=(pos, 0))), \
             patch.object(em, "_crypto_price", new=AsyncMock(return_value=100.0)), \
             patch.object(em, "_brain_levels", new=AsyncMock(return_value=None)), \
             patch.object(em, "_entry_price_fallback", new=AsyncMock(return_value=100.0)), \
             patch.object(em, "_mint_exit_receipt", return_value={"signature": "x"}), \
             patch("shared.exits.policy.get_policy", new=AsyncMock(return_value=_policy())):
            await em.run_once()  # adopt
            pos[0]["current_price"] = 109.0  # above +8% target
            with patch("shared.crypto.broker_adapter.get_kraken_adapter",
                       new=AsyncMock(return_value=fake)):
                await em.run_once()
            plan = await db[em.EXIT_PLANS].find_one({"symbol": SYM})
            assert plan["exit_reason"] == "take_profit"
            assert plan["exit_order"]["kind"] == "limit"
            assert not fake.market_calls
            (sym, qty, limit_price, side) = fake.limit_calls[0]
            # Marketable limit: just below trigger price, crossing spread.
            assert limit_price < 109.0
            assert limit_price > 108.5
            assert side == "SELL"
    finally:
        await _cleanup()


@pytest.mark.asyncio
async def test_stale_limit_exit_escalates_to_market():
    from db import db
    from datetime import timedelta
    await _cleanup()
    fake = _FakeKraken()
    stale_ts = em._iso(em._now() - timedelta(seconds=300))
    await db[em.EXIT_PLANS].insert_one({
        "plan_id": "esc-1", "lane": "crypto", "symbol": SYM,
        "status": "exiting", "exit_reason": "take_profit",
        "entry_price": 100.0, "stop_price": 97.0, "target_price": 108.0,
        "qty_held": 1.0, "max_hold_until": em._iso(), "attempts": 1,
        "reserved_at": stale_ts,
        "exit_order": {"order_id": "LIM-OLD", "kind": "limit",
                       "submitted_at": stale_ts, "qty": 1.0},
    })
    pos = [{"symbol": SYM, "qty": 1.0, "entry_price": None, "current_price": 108.5}]
    try:
        with patch.object(em, "_crypto_positions", new=AsyncMock(return_value=(pos, 0))), \
             patch.object(em, "_crypto_price", new=AsyncMock(return_value=108.5)), \
             patch.object(em, "_mint_exit_receipt", return_value={"signature": "x"}), \
             patch("shared.crypto.broker_adapter.get_kraken_adapter",
                   new=AsyncMock(return_value=fake)), \
             patch("shared.exits.policy.get_policy", new=AsyncMock(return_value=_policy())):
            await em.run_once()
        assert fake.cancelled == ["LIM-OLD"], "stale limit must be cancelled"
        assert fake.market_calls, "escalation must resubmit MARKET"
        plan = await db[em.EXIT_PLANS].find_one({"plan_id": "esc-1"})
        assert plan["exit_order"]["kind"] == "market"
        await _drain_outbox()
        receipts = await db[em.EXIT_RECEIPTS].find({"symbol": SYM}).to_list(20)
        assert any(r["event"] == "exit_escalated" for r in receipts)
    finally:
        await _cleanup()


@pytest.mark.asyncio
async def test_partial_fill_updates_held_qty_and_sells_remainder_only():
    from db import db
    await _cleanup()
    fake = _FakeKraken()
    # Plan reserved but submit previously failed (no exit_order) —
    # restart-recovery path sells the CURRENT broker qty (0.4 left
    # after a partial fill), not the original 1.0.
    await db[em.EXIT_PLANS].insert_one({
        "plan_id": "part-1", "lane": "crypto", "symbol": SYM,
        "status": "exiting", "exit_reason": "stop_loss",
        "entry_price": 100.0, "stop_price": 97.0, "target_price": 108.0,
        "qty_held": 1.0, "max_hold_until": em._iso(), "attempts": 1,
        "reserved_at": em._iso(), "exit_order": None,
    })
    pos = [{"symbol": SYM, "qty": 0.4, "entry_price": None, "current_price": 96.0}]
    try:
        with patch.object(em, "_crypto_positions", new=AsyncMock(return_value=(pos, 0))), \
             patch.object(em, "_crypto_price", new=AsyncMock(return_value=96.0)), \
             patch.object(em, "_mint_exit_receipt", return_value={"signature": "x"}), \
             patch("shared.crypto.broker_adapter.get_kraken_adapter",
                   new=AsyncMock(return_value=fake)), \
             patch("shared.exits.policy.get_policy", new=AsyncMock(return_value=_policy())):
            await em.run_once()
        assert fake.market_calls == [(SYM, 0.4, "SELL")], (
            "must sell broker-held qty, never the stale plan qty"
        )
    finally:
        await _cleanup()
