"""Tripwire — Paradox MA-canary plumbing.

Three checks lock in the canary contract:

  1. Strategy is pure: same closes → same MomentumSignal.
  2. Kill-switch fail-CLOSES — disabled env produces a structured
     refusal, no DB write.
  3. End-to-end: enabled canary + recent OHLCV → writes a
     shared_intents row with source=ma_canary, attributes to the
     CURRENT executor (not a hardcoded default), and the row carries
     enough fields for the auto-router to pick it up.
"""
from __future__ import annotations

import os
import pytest

from db import db
from namespaces import BRAIN_ROSTER, SHARED_INTENTS, SHARED_OHLCV_BARS
from shared.strategies.paradox_momentum_vote import (
    moving_average_momentum,
)


pytestmark = pytest.mark.asyncio


_SAMPLE_CLOSES_UPTREND = [
    180.0, 181, 182, 181, 183, 184, 185, 186, 187, 188,
    189, 190, 191, 192, 193, 194, 195, 196, 197, 198,
    199, 200, 201, 202, 203, 204, 205, 206, 207, 208,
    209, 210, 211, 212, 213, 214,
]


def test_strategy_pure_uptrend_produces_BUY():
    s = moving_average_momentum("AAPL", _SAMPLE_CLOSES_UPTREND, lane="equity")
    assert s.action == "BUY"
    assert s.confidence > 0
    assert s.reason == "fast_ma_above_slow_ma"
    assert s.fast_ma > s.slow_ma


def test_strategy_pure_short_input_produces_HOLD_not_enough_history():
    s = moving_average_momentum("AAPL", [100, 101, 102], lane="equity")
    assert s.action == "HOLD"
    assert s.confidence == 0.0
    assert s.reason == "not_enough_history"


def test_strategy_handles_nan_and_zero_inputs_safely():
    s = moving_average_momentum("AAPL", [float("nan"), 0, -1, *([100.0] * 40)], lane="equity")
    # NaN/zero/negative closes are filtered → 40 valid bars left, all 100 → flat MA
    assert s.action == "HOLD"
    assert s.reason in ("ma_gap_too_small", "ma_flat")


async def test_kill_switch_fail_closed_with_no_db_write():
    os.environ["PARADOX_MA_CANARY_ENABLED"] = "false"
    # Reload to pick up the env var change.
    from importlib import reload
    from shared.strategies import canary_runner
    reload(canary_runner)
    result = await canary_runner.fire_canary("AAPL", lane="equity")
    assert result["ok"] is False
    assert result["reason"] == "canary_kill_switch_disabled"
    # No row should have been written.
    n = await db[SHARED_INTENTS].count_documents({
        "source": "ma_canary",
        "symbol": "AAPL",
    })
    assert n == 0, f"Disabled canary wrote {n} rows — fail-CLOSED contract broken."


async def test_canary_attributes_to_current_executor_and_writes_intent():
    """End-to-end plumbing: enabled canary + warmup data → real
    shared_intents row attributed to the operator's current executor.

    Setup is isolated: we pin a known executor on the roster
    snapshot for the duration of the test, seed deterministic bars,
    fire once, then teardown.
    """
    SYM = "TEST/CANARY"
    snapshot = await db[BRAIN_ROSTER].find_one(
        {"_id": "current"}, {"_id": 0, "assignments": 1},
    )
    saved = ((snapshot or {}).get("assignments") or {}).copy()

    # Pin the equity executor to a known brain for this test.
    await db[BRAIN_ROSTER].update_one(
        {"_id": "current"},
        {"$set": {"assignments.executor": "barracuda"}},
        upsert=True,
    )

    # Seed 35 ascending bars at tf=1h so the strategy produces BUY.
    bars = []
    for i, price in enumerate(_SAMPLE_CLOSES_UPTREND):
        bars.append({
            "symbol": SYM, "tf": "1h", "source": "pytest_canary",
            "ts": f"2026-06-19T{i:02d}:00:00+00:00",
            "c": float(price), "o": float(price), "h": float(price), "l": float(price),
            "v": 100.0,
            "ingested_at": "2026-06-19T15:00:00+00:00",
        })
    await db[SHARED_OHLCV_BARS].insert_many(bars)

    try:
        os.environ["PARADOX_MA_CANARY_ENABLED"] = "true"
        from importlib import reload
        from shared.strategies import canary_runner
        reload(canary_runner)
        result = await canary_runner.fire_canary(SYM, lane="equity", timeframe="1h")

        assert result["ok"] is True, f"canary refused: {result}"
        assert result["action"] == "BUY"
        assert result["holder"] == "barracuda"
        assert result["intent_id"], "no intent_id returned"

        row = await db[SHARED_INTENTS].find_one(
            {"intent_id": result["intent_id"]}, {"_id": 0},
        )
        assert row is not None, "canary did not write a shared_intents row"
        assert row["source"] == "ma_canary"
        assert row["stack"] == "barracuda", (
            "canary must attribute to the CURRENT executor, not a "
            "hardcoded default (this is the GTO-as-auditor trap)."
        )
        assert row["gate_state"] == "pending"
        assert row["evidence"]["kill_switch"] == "PARADOX_MA_CANARY_ENABLED"
        assert row["evidence"]["paradox_rule"] == "strategy_testifies_never_executes"

    finally:
        # Cleanup test fixtures + restore roster.
        await db[SHARED_OHLCV_BARS].delete_many({"symbol": SYM})
        await db[SHARED_INTENTS].delete_many({"symbol": SYM})
        os.environ["PARADOX_MA_CANARY_ENABLED"] = "false"
        if saved:
            await db[BRAIN_ROSTER].update_one(
                {"_id": "current"}, {"$set": {"assignments": saved}},
            )
