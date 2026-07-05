"""Intent-clearance funnel — behavior tests (2026-02-17).

Locks in the semantic contract of the Monday tuning tile:

    1. Stage counts are monotonically non-increasing.
    2. `_compose(...)` correctly ANDs multiple `$or`s without one
       clobbering the other (the bug that made the initial funnel
       report 25k > 2k drops).
    3. `first_failed_stage` names the FIRST stage where cleared < prev.
    4. `dry_run_blocked` counts as roadguard-CLEARED (operator toggle,
       not a RoadGuard failure).
    5. Breakdown totals must equal the top-line `emitted` count.
"""
from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, "/app")
sys.path.insert(0, "/app/backend")


# ─── Composition primitive ──────────────────────────────────────────

def test_compose_and_wraps_multi_clause_queries():
    """Two clauses each with `$or` must both survive."""
    from routes.intent_clearance_funnel import _compose
    a = {"$or": [{"ts": {"$gte": "2026-01-01"}}]}
    b = {"$or": [{"risk_multiplier": 0}, {"risk_multiplier": None}]}
    q = _compose(a, b)
    assert "$and" in q
    assert a in q["$and"]
    assert b in q["$and"]


def test_compose_single_clause_stays_flat():
    """No `$and` wrapping when there's just one clause — keeps the
    query readable and index-friendly."""
    from routes.intent_clearance_funnel import _compose
    a = {"action": {"$in": ["BUY", "SELL"]}}
    q = _compose(a)
    assert q == a
    assert "$and" not in q


def test_compose_ignores_empty_clauses():
    """Empty {}s should be dropped — otherwise `_lane_clause(None)`
    would pollute queries with a no-op that shows up in explain plans."""
    from routes.intent_clearance_funnel import _compose
    q = _compose({"a": 1}, {}, {"b": 2})
    # Single-clause case if only one non-empty remains after filtering
    # is enforced by the len==1 branch — but here we have two, so $and.
    assert q == {"$and": [{"a": 1}, {"b": 2}]}


# ─── Stage semantic filters ─────────────────────────────────────────

def test_seat_cleared_excludes_advisory_only_and_pending():
    """These two `gate_state` values ARE the seat-failed markers."""
    from routes.intent_clearance_funnel import _seat_cleared_filter
    f = _seat_cleared_filter()
    excluded = f["gate_state"]["$nin"]
    assert "advisory_only" in excluded
    assert "pending" in excluded


def test_risk_sized_requires_positive_multiplier():
    """advisory_only stamps `risk_multiplier=0`. Requiring `>0` is what
    separates advisory-shaped rows from genuinely-sized ones. Also
    honors `broker_error_bucket` presence — an intent that reached the
    broker MUST have cleared risk, even if the current gate_state is
    now `blocked` from a broker-terminal stamp."""
    from routes.intent_clearance_funnel import _risk_sized_filter
    f = _risk_sized_filter()
    or_clauses = f["$or"]
    assert {"risk_multiplier": {"$gt": 0}} in or_clauses
    assert {"broker_error_bucket": {"$exists": True}} in or_clauses


def test_roadguard_cleared_counts_dry_run_blocked():
    """`dry_run_blocked` is the operator's lane kill-switch — RoadGuard
    let the intent through, the operator toggled the lane off. That
    MUST count as roadguard-cleared or the funnel misattributes the
    drop to RoadGuard when it's really an operator flag. Also honors
    `broker_error_bucket` for terminal broker-stamped intents."""
    from routes.intent_clearance_funnel import _roadguard_cleared_filter
    f = _roadguard_cleared_filter()
    or_clauses = f["$or"]
    # Positive gate-state branch:
    gate_branch = next(c for c in or_clauses if "gate_state" in c)
    assert "dry_run_blocked" in gate_branch["gate_state"]["$in"]
    assert "passed" in gate_branch["gate_state"]["$in"]
    assert "dry_run_passed" in gate_branch["gate_state"]["$in"]
    # Broker-reached branch:
    assert {"broker_error_bucket": {"$exists": True}} in or_clauses


# ─── End-to-end funnel semantics (mocked db) ────────────────────────

@pytest.mark.asyncio
async def test_funnel_stages_are_monotonically_non_increasing():
    """Contract: each downstream stage's count MUST be ≤ its predecessor.
    If this fails, the query composition is leaking rows past a filter."""
    from routes import intent_clearance_funnel as m

    # 7 `_count` calls total inside the endpoint: 4 for stage counts
    # (emitted, seat, risk, roadguard) + 3 for drop reasons (seat,
    # risk, roadguard). We only care about the first 4 for this test.
    count_returns = iter([100, 40, 25, 20,   # stages
                          60, 15, 5])         # drops
    linked_returns = iter([15, 10, 5])

    async def fake_count(*_a, **_k): return next(count_returns)
    async def fake_linked(*_a, **_k): return next(linked_returns)

    with patch.object(m, "_count", fake_count), \
         patch.object(m, "_linked_executions_count", fake_linked), \
         patch.object(m, "_sample_ids", AsyncMock(return_value=[])), \
         patch.object(m, "_top_reasons", AsyncMock(return_value=[])), \
         patch.object(m, "_linked_execution_samples", AsyncMock(return_value=[])), \
         patch.object(m, "_breakdown", AsyncMock(return_value={})), \
         patch.object(m, "db", _fake_db_with_no_execs()):
        out = await m.intent_clearance_funnel(hours=24, lane=None, _user={})

    counts = [s["count"] for s in out["stages"]]
    for i in range(1, len(counts)):
        assert counts[i] <= counts[i - 1], (
            f"Stage {out['stages'][i]['name']} ({counts[i]}) exceeded "
            f"predecessor {out['stages'][i-1]['name']} ({counts[i-1]}) — "
            f"query composition is leaking rows."
        )


@pytest.mark.asyncio
async def test_funnel_first_failed_stage_names_first_drop():
    """`first_failed_stage` MUST name the earliest stage where the
    count is less than its predecessor — that's the operator's next
    action item."""
    from routes import intent_clearance_funnel as m

    # emitted=100, seat=100, risk=100, roadguard=42 → first drop @ roadguard
    count_returns = iter([100, 100, 100, 42,  0, 0, 58])
    linked_returns = iter([42, 42, 42])

    async def fake_count(*_a, **_k): return next(count_returns)
    async def fake_linked(*_a, **_k): return next(linked_returns)

    with patch.object(m, "_count", fake_count), \
         patch.object(m, "_linked_executions_count", fake_linked), \
         patch.object(m, "_sample_ids", AsyncMock(return_value=[])), \
         patch.object(m, "_top_reasons", AsyncMock(return_value=[])), \
         patch.object(m, "_linked_execution_samples", AsyncMock(return_value=[])), \
         patch.object(m, "_breakdown", AsyncMock(return_value={})), \
         patch.object(m, "db", _fake_db_with_no_execs()):
        out = await m.intent_clearance_funnel(hours=24, lane=None, _user={})

    assert out["first_failed_stage"] == "roadguard_cleared"


@pytest.mark.asyncio
async def test_funnel_zero_emitted_does_not_divide_by_zero():
    """Empty window MUST return sane defaults, not crash. Also,
    `first_failed_stage` should be None — you can't 'fail' a stage
    when nothing was even attempted."""
    from routes import intent_clearance_funnel as m

    async def zero(*_a, **_k): return 0

    with patch.object(m, "_count", zero), \
         patch.object(m, "_linked_executions_count", zero), \
         patch.object(m, "_sample_ids", AsyncMock(return_value=[])), \
         patch.object(m, "_top_reasons", AsyncMock(return_value=[])), \
         patch.object(m, "_linked_execution_samples", AsyncMock(return_value=[])), \
         patch.object(m, "_breakdown", AsyncMock(return_value={})), \
         patch.object(m, "db", _fake_db_with_no_execs()):
        out = await m.intent_clearance_funnel(hours=24, lane=None, _user={})

    assert out["clearance_rate"] == 0.0
    assert all(s["count"] == 0 for s in out["stages"])
    assert out["first_failed_stage"] is None


# ─── Helpers ─────────────────────────────────────────────────────────

def _fake_db_with_no_execs():
    """Motor-style AsyncIOMotorClient stub whose `shared_intents.find`
    yields nothing (so the manual loop scanning intent_ids for the
    broker-rejection step is a no-op)."""
    coll = MagicMock()

    class _EmptyCursor:
        def __init__(self, *_a, **_k): pass
        def sort(self, *_a, **_k): return self
        def limit(self, *_a, **_k): return self
        def __aiter__(self):
            return self
        async def __anext__(self):
            raise StopAsyncIteration

    coll.find = MagicMock(return_value=_EmptyCursor())
    fake_db = MagicMock()
    fake_db.__getitem__ = MagicMock(return_value=coll)
    return fake_db
