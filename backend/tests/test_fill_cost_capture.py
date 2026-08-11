"""Fill Cost Capture + PairedCostFeed + recalibration guards
(2026-06 operator directive, priority order 1-3)."""
from __future__ import annotations

import sys

import pytest

sys.path.insert(0, "/app/backend")

from shared.execution_costs import _slip_pct, pair_round_trips  # noqa: E402
from shared.forensics.gate_v2_adapter import PairedCostFeed  # noqa: E402
from shared.forensics.promotion_gate_v2 import ExecutionFill  # noqa: E402

pytestmark = pytest.mark.tripwire


def _leg(sym, ts, side, fill, cost, fee=0.1):
    return {"symbol": sym, "ts": ts, "side": side, "status": "resolved",
            "fill_price": fill, "effective_leg_cost_pct": cost,
            "fee_pct": fee, "intent_id": f"i-{ts}"}


def test_slippage_is_side_aware():
    # BUY: paying above reference is positive slippage (a cost)
    assert _slip_pct("BUY", 101.0, 100.0) == pytest.approx(1.0)
    # SELL: receiving below reference is positive slippage (a cost)
    assert _slip_pct("SELL", 99.0, 100.0) == pytest.approx(1.0)
    assert _slip_pct("BUY", 100.0, None) is None


def test_fifo_pairing_exact_round_trip_cost():
    rows = [
        _leg("A/USD", "t1", "BUY", 100.0, 0.20),
        _leg("A/USD", "t2", "BUY", 102.0, 0.10),
        _leg("A/USD", "t3", "SELL", 105.0, 0.15),
        _leg("B/USD", "t4", "SELL", 50.0, 0.15),  # no open buy → unpaired
    ]
    pairs = pair_round_trips(rows)
    assert len(pairs) == 1
    p = pairs[0]
    assert p["symbol"] == "A/USD"
    assert p["buy_ts"] == "t1"  # FIFO: first buy pairs first
    assert p["round_trip_cost_pct"] == pytest.approx(0.35)
    assert p["gross_return_pct"] == pytest.approx(5.0)
    assert p["net_return_pct"] == pytest.approx(5.0 - 0.2)  # 2×0.1% fees


def test_unresolved_legs_excluded():
    rows = [
        {"symbol": "A/USD", "ts": "t1", "side": "BUY", "status": "pending",
         "fill_price": None},
        _leg("A/USD", "t2", "SELL", 105.0, 0.15),
    ]
    assert pair_round_trips(rows) == []


def _fill(i, liquidity="maker", fee=0.1, epoch="e1"):
    from datetime import datetime, timezone
    return ExecutionFill(
        fill_id=f"f{i}", trade_id=f"t{i}",
        timestamp=datetime.now(timezone.utc), side="BUY",
        fill_price=100.0, quantity=1.0, fee_pct=fee,
        reference_price=100.0, liquidity=liquidity, epoch_id=epoch)


def test_paired_feed_stays_assumed_below_min_fills():
    feed = PairedCostFeed(0.30, 30, pairs=[{"round_trip_cost_pct": 0.18}] * 10)
    est = feed.estimate([_fill(i) for i in range(5)], "e1")
    assert est.source == "assumed"
    assert est.round_trip_cost_pct == pytest.approx(0.30)


def test_paired_feed_uses_exact_pairs_once_measured():
    pairs = [{"round_trip_cost_pct": 0.18}] * 8
    feed = PairedCostFeed(0.30, 30, pairs=pairs)
    est = feed.estimate([_fill(i) for i in range(30)], "e1")
    assert est.source == "measured"
    assert est.round_trip_cost_pct == pytest.approx(0.18)


def test_paired_feed_falls_back_to_avg_leg_with_few_pairs():
    feed = PairedCostFeed(0.30, 30, pairs=[{"round_trip_cost_pct": 0.18}] * 2)
    est = feed.estimate([_fill(i) for i in range(30)], "e1")
    assert est.source == "measured"
    # 2× avg leg (fee 0.1 + slippage 0) = 0.2 — module's own estimate
    assert est.round_trip_cost_pct == pytest.approx(0.2)


def test_drawdown_criterion_renamed_everywhere():
    from shared.forensics.promotion_gate_v2 import (
        EvaluationObservation, PromotionConfig, PromotionGate,
    )
    from datetime import datetime, timezone
    obs = [EvaluationObservation(f"o{i}", datetime.now(timezone.utc), 1.0,
                                 "A/USD", "s", "e1") for i in range(120)]
    d = PromotionGate(PromotionConfig(min_observations=50,
                                      min_elapsed_hours=0)).evaluate(
        observations=obs, fills=[], epoch_id="e1")
    names = {c.name for c in d.criteria}
    assert "observation_drawdown_per_100" in names
    assert "drawdown_per_100_observations" not in names
