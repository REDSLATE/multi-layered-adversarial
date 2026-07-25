"""Gain Goal — operator profit objectives per lane (2026-07-25 spec).

Pins: window math, RTH-session pace, status precedence, drawdown
latch (no auto-resume, ack required, window rollover clears), the
binding risk-gate entry block (exits keep flowing), and the
reduce-only ahead-of-pace throttle.
"""
from __future__ import annotations

import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, "/app/backend")

from shared.goals import gain_goal, worker
from shared.hotpath import daily_spend, intent_queue, outbox, policy_snapshot

LANE = "crypto"


@pytest.fixture
def hp(tmp_path):
    import os
    outbox.reset_for_tests(str(tmp_path / "hp.sqlite"))
    policy_snapshot.reset_for_tests()
    daily_spend.reset_for_tests()
    intent_queue.reset_for_tests()
    yield
    outbox.reset_for_tests(os.environ.get("HOTPATH_DB_PATH", "/app/backend/data/hotpath.sqlite"))
    policy_snapshot.reset_for_tests()
    daily_spend.reset_for_tests()
    intent_queue.reset_for_tests()


@pytest.fixture
async def goal_flags(monkeypatch):
    """Snapshot + restore the gain_goals config and state docs, and
    point outcome reads at a scratch collection so live trades never
    leak into assertions."""
    from db import db
    monkeypatch.setattr(gain_goal, "EXIT_OUTCOMES", "gg_test_outcomes")
    await db["gg_test_outcomes"].delete_many({})
    saved = {}
    for fid in (gain_goal.FLAG_ID, gain_goal.STATE_FLAG_ID):
        saved[fid] = await db["runtime_flags"].find_one({"_id": fid})
        await db["runtime_flags"].delete_one({"_id": fid})
    yield db
    await db["gg_test_outcomes"].delete_many({})
    for fid, doc in saved.items():
        if doc is None:
            await db["runtime_flags"].delete_one({"_id": fid})
        else:
            await db["runtime_flags"].replace_one({"_id": fid}, doc, upsert=True)


async def _seed_outcomes(db, rows, tag):
    docs = []
    now = datetime.now(timezone.utc)
    for i, pnl in enumerate(rows):
        docs.append({
            "outcome_id": f"gg-test-{tag}-{i}-{uuid.uuid4()}",
            "lane": LANE,
            "brain": "camino" if i % 2 == 0 else "gto",
            "realized_pnl_usd": pnl,
            "entry_price": 0.0,   # zero turnover → net == gross (no fee noise)
            "exit_price": 0.0,
            "qty": 0.0,
            "closed_at": (now - timedelta(minutes=len(rows) - i)).isoformat(),
            "_gg_test_tag": tag,
        })
    await db[gain_goal.EXIT_OUTCOMES].insert_many(docs)
    return docs


async def _cleanup_outcomes(db, tag):
    await db[gain_goal.EXIT_OUTCOMES].delete_many({"_gg_test_tag": tag})


# ───────────────────────── window math ──────────────────────────────

def test_window_bounds_calendar_month():
    now = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)
    s, e, label = gain_goal.window_bounds("calendar_month", now)
    assert s == datetime(2026, 7, 1, tzinfo=timezone.utc)
    assert e == datetime(2026, 8, 1, tzinfo=timezone.utc)
    assert label == "JULY"


def test_window_bounds_calendar_week_and_rolling():
    now = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)  # Saturday
    s, e, _ = gain_goal.window_bounds("calendar_week", now)
    assert s == datetime(2026, 7, 20, tzinfo=timezone.utc)   # Monday
    assert (e - s).days == 7
    s2, e2, _ = gain_goal.window_bounds("rolling_days", now, rolling_days=7)
    assert e2 == now and (e2 - s2).days == 7


def test_rth_session_progress_counts_business_days():
    """July 2026: 22 eligible sessions (July 3 observed holiday
    excluded). On Saturday Jul 25 12:00 UTC all sessions through
    Fri Jul 24 are complete → 17 of 22."""
    s = datetime(2026, 7, 1, tzinfo=timezone.utc)
    e = datetime(2026, 8, 1, tzinfo=timezone.utc)
    now = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)
    p = gain_goal._rth_session_progress(s, e, now)
    assert p["total_sessions"] == 22
    assert p["completed_sessions"] == 17.0
    assert 0.76 < p["fraction"] < 0.79


# ─────────────────── evaluation + statuses ──────────────────────────

@pytest.mark.asyncio
async def test_no_goal_then_pace_statuses(hp, goal_flags):
    db = goal_flags
    tag = uuid.uuid4().hex[:8]
    try:
        cfg = await gain_goal.get_goal_config()
        ev = await gain_goal.evaluate_lane(LANE, cfg)
        assert ev["status"] == "NO_GOAL"

        # rolling window (fraction=1.0) → expected == target; seed a
        # sufficient sample well above target → GOAL_REACHED.
        await db["runtime_flags"].update_one(
            {"_id": gain_goal.FLAG_ID},
            {"$set": {f"{LANE}.target_net_pnl_usd": 50.0,
                      f"{LANE}.window_type": "rolling_days",
                      f"{LANE}.rolling_days": 1,
                      f"{LANE}.minimum_resolved_trades": 5}},
            upsert=True)
        await _seed_outcomes(db, [20.0, 15.0, 10.0, 8.0, 7.0], tag)
        cfg = await gain_goal.get_goal_config()
        ev = await gain_goal.evaluate_lane(LANE, cfg)
        assert ev["resolved_trades"] == 5
        assert ev["net_realized_pnl_usd"] == pytest.approx(60.0)
        assert ev["status"] == "GOAL_REACHED"
        assert ev["goal_progress_pct"] == pytest.approx(120.0)

        # raise the bar → BEHIND_PACE (rolling: expected = full target)
        await db["runtime_flags"].update_one(
            {"_id": gain_goal.FLAG_ID},
            {"$set": {f"{LANE}.target_net_pnl_usd": 500.0}})
        cfg = await gain_goal.get_goal_config()
        ev = await gain_goal.evaluate_lane(LANE, cfg)
        assert ev["status"] == "BEHIND_PACE"
        assert ev["pace_variance_usd"] == pytest.approx(60.0 - 500.0)

        # min trades above sample → INSUFFICIENT_SAMPLE outranks pace
        await db["runtime_flags"].update_one(
            {"_id": gain_goal.FLAG_ID},
            {"$set": {f"{LANE}.minimum_resolved_trades": 20}})
        cfg = await gain_goal.get_goal_config()
        ev = await gain_goal.evaluate_lane(LANE, cfg)
        assert ev["status"] == "INSUFFICIENT_SAMPLE"
        assert ev["sample_sufficient"] is False
    finally:
        await _cleanup_outcomes(db, tag)


@pytest.mark.asyncio
async def test_drawdown_breach_latch_block_and_ack(hp, goal_flags):
    """Peak +30 then -45 → max drawdown $45 ≥ $40 limit → BREACHED,
    entries blocked via snapshot, exits still flow, ack resumes, and a
    later profitable trade does NOT auto-resume."""
    from shared.risk.check import check
    db = goal_flags
    tag = uuid.uuid4().hex[:8]
    try:
        await db["runtime_flags"].update_one(
            {"_id": gain_goal.FLAG_ID},
            {"$set": {f"{LANE}.target_net_pnl_usd": 100.0,
                      f"{LANE}.window_type": "rolling_days",
                      f"{LANE}.rolling_days": 1,
                      f"{LANE}.maximum_window_drawdown_usd": 40.0,
                      f"{LANE}.minimum_resolved_trades": 3}},
            upsert=True)
        await _seed_outcomes(db, [30.0, -25.0, -20.0], tag)

        policy_snapshot._dirty = False  # noqa: SLF001
        policy_snapshot.apply_local(
            master_switch_enabled=True,
            lane_enabled={"equity": True, "crypto": True},
        )
        result = await worker.evaluate_and_publish()
        ev = result["lanes"][LANE]
        assert ev["status"] == "DRAWDOWN_BREACHED"
        assert ev["max_window_drawdown_usd"] == pytest.approx(45.0)
        assert ev["entries_blocked"] is True
        assert "45.00" in ev["breach_latch"]["reason"]

        # binding gate: BUY blocked, SELL (exit) flows
        r_buy = await check({"intent_id": "gg-b1", "lane": LANE, "action": "BUY"},
                            notional_usd=5.0)
        assert r_buy.ok is False
        assert r_buy.reason == f"gain_goal_drawdown_breach:{LANE}"
        r_sell = await check({"intent_id": "gg-s1", "lane": LANE, "action": "SELL"},
                             notional_usd=5.0)
        assert r_sell.ok is True

        # a later winning trade must NOT auto-resume (latch holds)
        await _seed_outcomes(db, [60.0], tag)
        result = await worker.evaluate_and_publish()
        assert result["lanes"][LANE]["entries_blocked"] is True

        # operator ack resumes entries
        ack = await worker.acknowledge_breach(LANE, "operator@test")
        assert ack["ok"] is True
        r_buy2 = await check({"intent_id": "gg-b2", "lane": LANE, "action": "BUY"},
                             notional_usd=5.0)
        assert r_buy2.ok is True
    finally:
        await _cleanup_outcomes(db, tag)


@pytest.mark.asyncio
async def test_ahead_of_pace_throttle_reduce_only(hp, goal_flags):
    """Goal reached (≥ activation 100%) → throttle multiplier is
    published; below activation → no throttle."""
    db = goal_flags
    tag = uuid.uuid4().hex[:8]
    try:
        await db["runtime_flags"].update_one(
            {"_id": gain_goal.FLAG_ID},
            {"$set": {f"{LANE}.target_net_pnl_usd": 50.0,
                      f"{LANE}.window_type": "rolling_days",
                      f"{LANE}.rolling_days": 1,
                      f"{LANE}.minimum_resolved_trades": 1}},
            upsert=True)
        await _seed_outcomes(db, [60.0], tag)
        policy_snapshot._dirty = False  # noqa: SLF001
        result = await worker.evaluate_and_publish()
        assert result["lanes"][LANE]["throttle_active_multiplier"] == 0.5
        snap = policy_snapshot.get()
        assert (snap["gain_goal"]["throttle"] or {}).get(LANE) == 0.5

        # progress below activation → throttle off
        await db["runtime_flags"].update_one(
            {"_id": gain_goal.FLAG_ID},
            {"$set": {f"{LANE}.target_net_pnl_usd": 500.0}})
        result = await worker.evaluate_and_publish()
        assert result["lanes"][LANE]["throttle_active_multiplier"] is None
        assert policy_snapshot.get()["gain_goal"]["throttle"].get(LANE) is None
    finally:
        await _cleanup_outcomes(db, tag)


@pytest.mark.asyncio
async def test_pct_target_and_brain_attribution(hp, goal_flags):
    """Percent target converts via deployed capital; brains get
    attribution rows but NO quotas — nothing brain-facing is written."""
    from namespaces import CAPITAL_LEDGER
    db = goal_flags
    tag = uuid.uuid4().hex[:8]
    ledger_saved = await db[CAPITAL_LEDGER].find_one({"_id": f"{LANE}_cap"})
    try:
        await db[CAPITAL_LEDGER].update_one(
            {"_id": f"{LANE}_cap"}, {"$set": {"total": 1000.0}}, upsert=True)
        await db["runtime_flags"].update_one(
            {"_id": gain_goal.FLAG_ID},
            {"$set": {f"{LANE}.target_return_pct": 2.0,
                      f"{LANE}.primary_metric": "net_return_on_deployed_capital_pct",
                      f"{LANE}.window_type": "rolling_days",
                      f"{LANE}.rolling_days": 1,
                      f"{LANE}.minimum_resolved_trades": 2}},
            upsert=True)
        await _seed_outcomes(db, [15.0, 10.0], tag)
        cfg = await gain_goal.get_goal_config()
        ev = await gain_goal.evaluate_lane(LANE, cfg)
        assert ev["effective_target_usd"] == pytest.approx(20.0)  # 2% of $1000
        assert ev["return_on_deployed_pct"] == pytest.approx(2.5)
        assert ev["status"] == "GOAL_REACHED"
        brains = {b["brain"]: b for b in ev["brain_attribution"]}
        assert brains["camino"]["net_pnl_usd"] == pytest.approx(15.0)
        assert brains["gto"]["net_pnl_usd"] == pytest.approx(10.0)
        assert "target" not in brains["camino"]  # no per-brain quotas
    finally:
        await _cleanup_outcomes(db, tag)
        if ledger_saved is None:
            await db[CAPITAL_LEDGER].delete_one({"_id": f"{LANE}_cap"})
        else:
            await db[CAPITAL_LEDGER].replace_one(
                {"_id": f"{LANE}_cap"}, ledger_saved, upsert=True)


@pytest.mark.asyncio
async def test_window_rollover_clears_latch(hp, goal_flags):
    """The latch pins the breach window_start — a NEW window is the
    configured reset."""
    db = goal_flags
    await db["runtime_flags"].update_one(
        {"_id": gain_goal.STATE_FLAG_ID},
        {"$set": {"latches": {LANE: {
            "breached": True, "window_start": "2020-01-01T00:00:00+00:00",
            "reason": "old window", "acked_at": None}}}},
        upsert=True)
    await db["runtime_flags"].update_one(
        {"_id": gain_goal.FLAG_ID},
        {"$set": {f"{LANE}.target_net_pnl_usd": 100.0,
                  f"{LANE}.maximum_window_drawdown_usd": 40.0}},
        upsert=True)
    policy_snapshot._dirty = False  # noqa: SLF001
    result = await worker.evaluate_and_publish()
    assert result["lanes"][LANE]["entries_blocked"] is False
    assert policy_snapshot.get()["gain_goal"]["block"].get(LANE) is False
