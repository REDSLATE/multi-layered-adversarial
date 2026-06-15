"""Regression: the auto-submit chain MUST always write an audit row.

The 2026-02-20 production incident: 2,586 intents "Never submitted
(no audit row)" — they were in dry_run_passed state but `maybe_auto_submit`
either silently returned (intent_id missing) or raised an exception
before its internal try/except could write `auto_submit_failed`.
Result: zero audit signal for a leak draining the funnel.

These tests pin the two new audit guarantees:

  1. `maybe_auto_submit(intent_id)` where intent_id is not in
     shared_intents → writes `auto_submit_skipped` with
     `skip_category=intent_not_found`. Never returns silently.

  2. `_run_dry_run_then_auto_submit` where the inner pipeline raises
     before maybe_auto_submit can write its own row → writes
     `auto_submit_failed` with `skip_category=internal_error` and a
     `phase` field so the operator can tell where the failure
     happened (in_dry_run vs post_dry_run).
"""
from __future__ import annotations

import pytest

from db import db
from namespaces import SHARED_GATE_RESULTS, SHARED_INTENTS
from shared.auto_submit_policy import maybe_auto_submit
from shared.intents import _run_dry_run_then_auto_submit


@pytest.fixture
async def clean():
    await db[SHARED_INTENTS].delete_many({"intent_id": {"$regex": "^chain_audit_"}})
    await db[SHARED_GATE_RESULTS].delete_many({"intent_id": {"$regex": "^chain_audit_"}})
    yield
    await db[SHARED_INTENTS].delete_many({"intent_id": {"$regex": "^chain_audit_"}})
    await db[SHARED_GATE_RESULTS].delete_many({"intent_id": {"$regex": "^chain_audit_"}})


@pytest.mark.asyncio
async def test_maybe_auto_submit_writes_audit_when_intent_missing(clean):
    """intent_id with no matching row in shared_intents must still
    produce an `auto_submit_skipped` row with skip_category=intent_not_found.
    """
    result = await maybe_auto_submit("chain_audit_missing_1")
    assert result is None
    row = await db[SHARED_GATE_RESULTS].find_one({"intent_id": "chain_audit_missing_1"})
    assert row is not None, "expected an audit row for the missing intent"
    assert row["kind"] == "auto_submit_skipped"
    assert row["skip_category"] == "intent_not_found"


@pytest.mark.asyncio
async def test_chain_writes_audit_when_inner_raises(clean, monkeypatch):
    """If `run_dry_run_for_intent` raises before maybe_auto_submit can
    write its own audit row, the chain's catch-all must write an
    `auto_submit_failed` row with phase=in_dry_run.
    """
    boom = RuntimeError("simulated dry-run crash")

    async def fake_dry_run(*_args, **_kwargs):
        raise boom

    # Patch the symbol the chain imports lazily.
    import shared.execution as exec_mod
    monkeypatch.setattr(exec_mod, "run_dry_run_for_intent", fake_dry_run)

    await _run_dry_run_then_auto_submit("chain_audit_boom_1", actor="test")
    row = await db[SHARED_GATE_RESULTS].find_one({"intent_id": "chain_audit_boom_1"})
    assert row is not None, "expected catch-all audit row"
    assert row["kind"] == "auto_submit_failed"
    assert row["skip_category"] == "internal_error"
    assert row["phase"] == "in_dry_run"
    assert "simulated dry-run crash" in row["reason"]


@pytest.mark.asyncio
async def test_chain_marks_phase_post_dry_run_when_submit_step_raises(clean, monkeypatch):
    """Dry-run succeeds → maybe_auto_submit raises (without writing
    its own row) → catch-all must mark phase=post_dry_run so the
    operator can localize the bug to the auto-submit stage.
    """
    async def fake_dry_run(*_args, **_kwargs):
        return None  # success — no exception

    async def boom_submit(*_args, **_kwargs):
        raise RuntimeError("simulated submit-stage crash")

    import shared.execution as exec_mod
    import shared.auto_submit_policy as policy_mod
    monkeypatch.setattr(exec_mod, "run_dry_run_for_intent", fake_dry_run)
    monkeypatch.setattr(policy_mod, "maybe_auto_submit", boom_submit)

    await _run_dry_run_then_auto_submit("chain_audit_boom_2", actor="test")
    row = await db[SHARED_GATE_RESULTS].find_one({"intent_id": "chain_audit_boom_2"})
    assert row is not None
    assert row["kind"] == "auto_submit_failed"
    assert row["skip_category"] == "internal_error"
    assert row["phase"] == "post_dry_run"
    assert "simulated submit-stage crash" in row["reason"]
