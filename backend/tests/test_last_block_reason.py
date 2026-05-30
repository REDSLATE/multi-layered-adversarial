"""Tests for GET /api/admin/execution/last-block-reason.

Doctrine: read-only diagnostic surfacing the first failing gate for
recent dry_run_blocked / blocked / rejected_at_ingest intents.
Operator uses this when "intents emitted but no trades fire" — turns
a 5-minute scroll-the-receipts hunt into a single API call.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from db import db
from namespaces import SHARED_INTENTS, SHARED_GATE_RESULTS
from shared.execution import last_block_reason


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _intent_doc(intent_id: str, stack: str, action: str, gate_state: str, lane: str = "equity"):
    return {
        "intent_id": intent_id,
        "stack": stack,
        "symbol": "AAPL",
        "action": action,
        "lane": lane,
        "gate_state": gate_state,
        "ingest_ts": _now_iso(),
    }


def _gate_doc(intent_id: str, first_fail_name: str, reason: str):
    return {
        "intent_id": intent_id,
        "kind": "dry_run",
        "ts": _now_iso(),
        "gates": [
            {"name": "schema_invariants", "passed": True, "reason": "pinned"},
            {"name": "action_routable", "passed": True, "reason": "BUY routable"},
            {"name": first_fail_name, "passed": False, "reason": reason},
        ],
    }


@pytest.fixture
async def seed_blocked():
    """Seed two routable + one HOLD blocked intent. Yields the intent_ids
    so individual tests can target the same fixture data. Uses a unique
    stack name (`lbrtest-<uuid>`) so the fixture is isolated from any
    real intents already in the DB."""
    test_stack = f"lbrtest-{uuid.uuid4().hex[:8]}"
    ids = []
    for action, gate, reason in [
        ("BUY", "broker_connected", "no broker configured for lane='equity'"),
        ("SELL", "executor_seat_check", "executor seat is vacant"),
        ("HOLD", "action_routable", "HOLD is a watchlist signal"),
    ]:
        iid = f"lbr-test-{uuid.uuid4().hex[:10]}"
        await db[SHARED_INTENTS].insert_one(
            _intent_doc(iid, test_stack, action, "dry_run_blocked")
        )
        await db[SHARED_GATE_RESULTS].insert_one(_gate_doc(iid, gate, reason))
        ids.append(iid)
    yield {"stack": test_stack, "ids": ids}
    await db[SHARED_INTENTS].delete_many({"intent_id": {"$in": ids}})
    await db[SHARED_GATE_RESULTS].delete_many({"intent_id": {"$in": ids}})


@pytest.mark.asyncio
async def test_last_block_reason_excludes_hold_by_default(seed_blocked):
    """HOLD intents are watchlist signals, NOT trade attempts. They must
    NOT pollute the operator's blocked-trade view by default."""
    res = await last_block_reason(
        stack=seed_blocked["stack"], limit=20, include_hold=False, _user={"email": "test"},
    )
    actions = [r["action"] for r in res["items"]]
    assert "HOLD" not in actions, "HOLDs leaked into routable-only view"
    assert set(actions) <= {"BUY", "SELL", "SHORT", "COVER"}


@pytest.mark.asyncio
async def test_last_block_reason_surfaces_first_failing_gate(seed_blocked):
    """For each blocked intent, the endpoint must return the FIRST
    gate row with passed=False — that's the actual blocker."""
    res = await last_block_reason(
        stack=seed_blocked["stack"], limit=20, include_hold=False, _user={"email": "test"},
    )
    gates_seen = {r["first_failing_gate"] for r in res["items"]}
    assert "broker_connected" in gates_seen
    assert "executor_seat_check" in gates_seen
    # No passing gate should appear as a "failing gate".
    assert "schema_invariants" not in gates_seen


@pytest.mark.asyncio
async def test_last_block_reason_include_hold_flag_surfaces_them(seed_blocked):
    """include_hold=True opts in to seeing HOLD watchlist signals."""
    res = await last_block_reason(
        stack=seed_blocked["stack"], limit=20, include_hold=True, _user={"email": "test"},
    )
    actions = [r["action"] for r in res["items"]]
    assert "HOLD" in actions


@pytest.mark.asyncio
async def test_last_block_reason_summary_counts(seed_blocked):
    """Summary aggregates first-failing-gate counts in descending order."""
    res = await last_block_reason(
        stack=seed_blocked["stack"], limit=20, include_hold=False, _user={"email": "test"},
    )
    summary = res["summary_by_failing_gate"]
    counts = {row["gate"]: row["n"] for row in summary}
    assert counts.get("broker_connected") == 1
    assert counts.get("executor_seat_check") == 1
