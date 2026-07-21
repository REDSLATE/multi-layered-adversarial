"""Exit outcomes → brain DAWE learning loop (2026-07-22)."""
from __future__ import annotations

import sys
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, "/app/backend")

from shared.exits import outcomes as oc


def _plan(**over):
    base = {
        "plan_id": "oc-1", "lane": "crypto", "symbol": "OUTC/USD",
        "status": "closed", "entry_price": 100.0, "qty_held": 2.0,
        "exit_reason": "stop_loss", "exit_price_est": 96.5,
        "origin_stack": "hellcat", "origin_intent_id": "int-1",
        "levels_source": "lane_default", "adopted_at": "2026-07-22T00:00:00+00:00",
    }
    base.update(over)
    return base


# ── outcome labeling ────────────────────────────────────────────────

def test_labels_map_operator_taxonomy():
    assert oc._label(_plan(exit_reason="take_profit")) == "tp_hit"
    assert oc._label(_plan(exit_reason="stop_loss")) == "sl_hit"
    assert oc._label(_plan(exit_reason="max_hold")) == "timeout"
    assert oc._label(_plan(exit_reason="manual_close")) == "manual"
    assert oc._label(
        _plan(close_detail="position_closed_externally", exit_reason=None)
    ) == "external"


# ── ledger + fold ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_record_outcome_writes_ledger_and_folds_dawe():
    from db import db
    await db[oc.EXIT_OUTCOMES].delete_many({"symbol": "OUTC/USD"})
    try:
        with patch.object(oc, "_fold_into_dawe", new=AsyncMock(return_value=True)) as fold:
            row = await oc.record_outcome(_plan())
        assert row["outcome"] == "sl_hit"
        assert row["brain"] == "hellcat"
        assert row["realized_pnl_pct"] == pytest.approx(-3.5)
        assert row["realized_pnl_usd"] == pytest.approx(-7.0)
        assert row["dawe_folded"] is True
        fold.assert_awaited_once_with("hellcat", "crypto", pytest.approx(-0.035))
        stored = await db[oc.EXIT_OUTCOMES].find_one({"symbol": "OUTC/USD"})
        assert stored and stored["outcome"] == "sl_hit"
    finally:
        await db[oc.EXIT_OUTCOMES].delete_many({"symbol": "OUTC/USD"})


@pytest.mark.asyncio
async def test_external_close_without_price_skips_fold():
    from db import db
    await db[oc.EXIT_OUTCOMES].delete_many({"symbol": "OUTC/USD"})
    try:
        with patch.object(oc, "_fold_into_dawe", new=AsyncMock()) as fold:
            row = await oc.record_outcome(_plan(
                exit_reason=None, exit_price_est=None,
                close_detail="position_closed_externally",
            ))
        assert row["outcome"] == "external"
        assert row["realized_pnl_pct"] is None
        assert row["dawe_folded"] is False
        fold.assert_not_awaited()
    finally:
        await db[oc.EXIT_OUTCOMES].delete_many({"symbol": "OUTC/USD"})


@pytest.mark.asyncio
async def test_unattributed_plan_still_writes_ledger_no_fold():
    from db import db
    await db[oc.EXIT_OUTCOMES].delete_many({"symbol": "OUTC/USD"})
    try:
        with patch.object(oc, "_fold_into_dawe", new=AsyncMock()) as fold:
            row = await oc.record_outcome(_plan(origin_stack=None))
        assert row["brain"] is None
        assert row["realized_pnl_pct"] == pytest.approx(-3.5)
        fold.assert_not_awaited()
    finally:
        await db[oc.EXIT_OUTCOMES].delete_many({"symbol": "OUTC/USD"})


@pytest.mark.asyncio
async def test_dawe_fold_moves_session_weight_same_path_as_grader():
    """A full TP hit (+8% on crypto, tp band 8%) grades quality 1.0
    and must RAISE session_weight; a full SL (−3%) must LOWER it.
    Uses the real dawe math against a throwaway brain key."""
    from db import db
    from mc_arbiter.arbiter import BRM, STACK_ID, load_dawe
    brain = "test_exit_fold_brain"
    unset = {f"brains.{brain}": 1}
    await db[BRM].update_one({"_id": STACK_ID}, {"$unset": unset})
    try:
        before = (await load_dawe(brain, "crypto")).session_weight
        assert await oc._fold_into_dawe(brain, "crypto", 0.08) is True
        after_tp = (await load_dawe(brain, "crypto")).session_weight
        assert after_tp > before, "full TP must raise session weight"

        assert await oc._fold_into_dawe(brain, "crypto", -0.03) is True
        after_sl = (await load_dawe(brain, "crypto")).session_weight
        assert after_sl < after_tp, "SL must lower session weight"
    finally:
        await db[BRM].update_one({"_id": STACK_ID}, {"$unset": unset})


# ── scorecard aggregate ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_brain_scorecard_aggregates_by_brain_lane():
    from db import db
    await db[oc.EXIT_OUTCOMES].delete_many({"symbol": "SCORE/USD"})
    now = "2026-07-22T00:00:00+00:00"
    rows = [
        {"symbol": "SCORE/USD", "lane": "crypto", "brain": "gto",
         "outcome": "tp_hit", "realized_pnl_pct": 8.0,
         "realized_pnl_usd": 0.4, "closed_at": now, "plan_id": f"sc-{i}"}
        for i in range(2)
    ] + [
        {"symbol": "SCORE/USD", "lane": "crypto", "brain": "gto",
         "outcome": "sl_hit", "realized_pnl_pct": -3.0,
         "realized_pnl_usd": -0.15, "closed_at": now, "plan_id": "sc-3"},
    ]
    await db[oc.EXIT_OUTCOMES].insert_many(rows)
    try:
        card = await oc.brain_scorecard()
        gto = next(
            s for s in card if s["brain"] == "gto" and s["lane"] == "crypto"
        )
        assert gto["closed"] >= 3
        assert gto["tp_hit"] >= 2
        assert gto["sl_hit"] >= 1
    finally:
        await db[oc.EXIT_OUTCOMES].delete_many({"symbol": "SCORE/USD"})
