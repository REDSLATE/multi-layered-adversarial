"""Broker-fill reconciliation tests (2026-06 operator directive) —
FIFO pairing with partial fills, multi-leg exits, orphan sells,
repeat-intent dedup wiring."""
from __future__ import annotations

import sys

import pytest

sys.path.insert(0, "/app/backend")

from shared.reconciliation import _kraken_pair_to_symbol, pair_fills  # noqa: E402

pytestmark = pytest.mark.tripwire


def _fill(fid, side, qty, price, *, sym="PUMP/USD", fee=0.01, ts="t0",
          maker=True, intent=None):
    return {"_id": fid, "lane": "crypto", "symbol": sym, "side": side,
            "qty": qty, "price": price, "fee_usd": fee, "ts": ts,
            "maker": maker,
            "link": {"intent_id": intent, "brain": "camino",
                     "stack": "momentum"} if intent else {}}


def test_simple_round_trip_pnl_and_fees():
    outcomes, orphans = pair_fills([
        _fill("b1", "BUY", 1.0, 100.0, ts="t1", fee=0.10, intent="i1"),
        _fill("s1", "SELL", 1.0, 105.0, ts="t2", fee=0.11),
    ])
    assert not orphans and len(outcomes) == 1
    o = outcomes[0]
    assert o["realized_pnl_usd"] == pytest.approx(5.0 - 0.21)
    assert o["gross_return_pct"] == pytest.approx(5.0)
    assert o["fees_usd"] == pytest.approx(0.21)
    assert o["fee_pct"] == pytest.approx(0.21)  # 0.21/100 notional
    assert o["intent_id"] == "i1" and o["brain"] == "camino"
    assert o["measured_cost_eligible"] is True
    assert o["_id"] == "rt:s1"  # deterministic — idempotent upserts


def test_partial_fills_and_multi_leg_exits():
    # one buy of 2.0, exited in two legs of 1.2 and 0.8
    outcomes, orphans = pair_fills([
        _fill("b1", "BUY", 2.0, 100.0, ts="t1", fee=0.20),
        _fill("s1", "SELL", 1.2, 110.0, ts="t2", fee=0.13),
        _fill("s2", "SELL", 0.8, 90.0, ts="t3", fee=0.07),
    ])
    assert not orphans and len(outcomes) == 2
    o1, o2 = outcomes
    assert o1["qty"] == pytest.approx(1.2)
    # entry fee allocated pro-rata: 0.20 × (1.2/2.0) = 0.12
    assert o1["fees_usd"] == pytest.approx(0.12 + 0.13)
    assert o1["realized_pnl_usd"] == pytest.approx(12.0 - 0.25)
    assert o2["qty"] == pytest.approx(0.8)
    assert o2["realized_pnl_usd"] == pytest.approx(-8.0 - (0.08 + 0.07))


def test_fifo_across_multiple_lots():
    # sell 1.5 consumes lot1 (1.0 @ 100) fully + lot2 (1.0 @ 110) half
    outcomes, _ = pair_fills([
        _fill("b1", "BUY", 1.0, 100.0, ts="t1", fee=0.0),
        _fill("b2", "BUY", 1.0, 110.0, ts="t2", fee=0.0),
        _fill("s1", "SELL", 1.5, 120.0, ts="t3", fee=0.0),
    ])
    o = outcomes[0]
    assert o["entry_fill_ids"] == ["b1", "b2"]
    assert o["entry_avg_price"] == pytest.approx((100 + 110 * 0.5) / 1.5)
    assert o["realized_pnl_usd"] == pytest.approx(
        (120 - (100 + 55) / 1.5) * 1.5)


def test_orphan_sell_is_exception_not_silent_drop():
    outcomes, orphans = pair_fills([
        _fill("s1", "SELL", 1.0, 50.0, sym="XAN/USD"),
    ])
    assert outcomes == []
    assert len(orphans) == 1 and orphans[0]["_id"] == "s1"


def test_symbols_and_lanes_never_cross():
    outcomes, orphans = pair_fills([
        _fill("b1", "BUY", 1.0, 100.0, sym="A/USD", ts="t1"),
        _fill("s1", "SELL", 1.0, 105.0, sym="B/USD", ts="t2"),
    ])
    assert outcomes == [] and len(orphans) == 1


def test_kraken_pair_normalization():
    assert _kraken_pair_to_symbol("XXBTZUSD") == "BTC/USD"
    assert _kraken_pair_to_symbol("XDGUSD") == "DOGE/USD"
    assert _kraken_pair_to_symbol("PUMPUSD") == "PUMP/USD"
    assert _kraken_pair_to_symbol("XETHZUSD") == "ETH/USD"


@pytest.mark.asyncio
async def test_repeat_dedup_config_defaults():
    from shared.risk_sizer.missed_entries import DEFAULTS
    assert DEFAULTS["repeat_window_min"] == 60.0


def test_wiring():
    src = open("/app/backend/shared/risk_sizer/missed_entries.py").read()
    assert "repeat_suppressed" in src and "repeat_count" in src
    life = open("/app/backend/server_modules/lifespan.py").read()
    assert "reconciliation" in life
    adapter = open("/app/backend/shared/forensics/gate_v2_adapter.py").read()
    assert "broker_fills_ledger" in adapter and "trade_outcomes" in adapter
