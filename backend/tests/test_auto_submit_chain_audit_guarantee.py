"""Regression: the auto-submit chain MUST always write an audit row.

The 2026-02-20 production incident: 2,586 intents "Never submitted
(no audit row)" — they were in dry_run_passed state but `maybe_auto_submit`
either silently returned (intent_id missing) or raised an exception
before its internal try/except could write `auto_submit_failed`.
Result: zero audit signal for a leak draining the funnel.

These tests pin the audit-completeness contract: EVERY call to
maybe_auto_submit produces exactly one of:
  * auto_submit_skipped   — Shelly filter said no
  * auto_submit_failed    — submit_raised / execution_path_leak
  * auto_submit_submitted — handed off to broker (verdict captured)
  * auto_submit_exception — unmapped exception (then re-raised)
"""
from __future__ import annotations

import pytest

from db import db
from namespaces import SHARED_GATE_RESULTS, SHARED_INTENTS
from shared.auto_submit_policy import (
    maybe_auto_submit,
    reset_policy_for_tests,
    set_policy,
)
from shared.intents import _run_dry_run_then_auto_submit


@pytest.fixture
async def clean():
    await db[SHARED_INTENTS].delete_many({"intent_id": {"$regex": "^chain_audit_"}})
    await db[SHARED_GATE_RESULTS].delete_many({"intent_id": {"$regex": "^chain_audit_"}})
    reset_policy_for_tests()
    yield
    await db[SHARED_INTENTS].delete_many({"intent_id": {"$regex": "^chain_audit_"}})
    await db[SHARED_GATE_RESULTS].delete_many({"intent_id": {"$regex": "^chain_audit_"}})
    reset_policy_for_tests()


async def _seed_eligible_intent(intent_id: str) -> None:
    """Seed an intent that matches_tier_1 will accept (Shelly enabled,
    BUY equity at confidence 0.95, dry_run_passed)."""
    set_policy(True)  # enable Shelly
    await db[SHARED_INTENTS].insert_one({
        "intent_id": intent_id,
        "stack": "alpha",
        "lane": "equity",
        "action": "BUY",
        "symbol": "AAL",
        "confidence": 0.95,
        "dry_run_state": "passed",
        "executed": False,
    })


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
        return None

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


@pytest.mark.asyncio
async def test_submit_raised_writes_skip_category(clean, monkeypatch):
    """When `execution_submit` raises inside maybe_auto_submit, the
    failure row must carry skip_category='submit_raised' so the
    post-mortem can distinguish broker raises from chain leaks."""
    await _seed_eligible_intent("chain_audit_submit_raise_1")

    async def boom_submit(_body, user=None):
        raise RuntimeError("simulated broker crash")

    import shared.auto_submit_policy as policy_mod
    monkeypatch.setattr(policy_mod, "execution_submit", boom_submit, raising=False)
    import shared.execution as exec_mod
    monkeypatch.setattr(exec_mod, "execution_submit", boom_submit)

    result = await maybe_auto_submit("chain_audit_submit_raise_1")
    assert result is None
    row = await db[SHARED_GATE_RESULTS].find_one(
        {"intent_id": "chain_audit_submit_raise_1"},
        sort=[("ts", -1)],
    )
    assert row is not None
    assert row["kind"] == "auto_submit_failed"
    assert row["skip_category"] == "submit_raised"
    assert "simulated broker crash" in row["reason"]


@pytest.mark.asyncio
async def test_execution_path_leak_when_submit_returns_none(clean, monkeypatch):
    """If execution_submit returns None (no exception, no row), the
    intent would silently leak. The leak-guard must write an
    `auto_submit_failed` row with skip_category='execution_path_leak'.
    THIS is the row that closes the 2,586-ghost-intent black hole."""
    await _seed_eligible_intent("chain_audit_leak_1")

    async def returns_none(_body, user=None):
        return None  # the leak: no raise, no row, just None

    import shared.execution as exec_mod
    monkeypatch.setattr(exec_mod, "execution_submit", returns_none)

    result = await maybe_auto_submit("chain_audit_leak_1")
    assert result is None
    row = await db[SHARED_GATE_RESULTS].find_one(
        {"intent_id": "chain_audit_leak_1"},
        sort=[("ts", -1)],
    )
    assert row is not None, "execution_path_leak must produce a row"
    assert row["kind"] == "auto_submit_failed"
    assert row["skip_category"] == "execution_path_leak"
    assert row["reason"] == "eligible_but_no_submit_path"


@pytest.mark.asyncio
async def test_success_path_writes_auto_submit_submitted(clean, monkeypatch):
    """The success path must write an `auto_submit_submitted` row
    capturing the broker verdict. This gives the operator a clear
    'Shelly handed off, here's what happened next' signal that's
    distinct from execution_submit's own audit row."""
    await _seed_eligible_intent("chain_audit_success_1")

    async def passes_submit(_body, user=None):
        return {"verdict": "passed", "executed": True}

    import shared.execution as exec_mod
    monkeypatch.setattr(exec_mod, "execution_submit", passes_submit)

    result = await maybe_auto_submit("chain_audit_success_1")
    assert result == {"verdict": "passed", "executed": True}
    row = await db[SHARED_GATE_RESULTS].find_one(
        {"intent_id": "chain_audit_success_1", "kind": "auto_submit_submitted"},
    )
    assert row is not None
    assert row["submit_verdict"] == "passed"
    assert row["executed"] is True


@pytest.mark.asyncio
async def test_unexpected_exception_writes_auto_submit_exception_and_reraises(clean, monkeypatch):
    """If anything unexpected raises in the body that isn't caught by
    the inner try blocks, the outer wrapper must write an
    `auto_submit_exception` row AND re-raise (so the chain catch-all
    above can also see it). This is the final safety net."""
    await _seed_eligible_intent("chain_audit_unexpected_1")

    def boom_categorize(_reason):
        raise RuntimeError("simulated upstream code bug")

    import shared.auto_submit_policy as policy_mod
    # Force matches_tier_1 to reject so we hit _categorize_skip,
    # which we monkeypatched to blow up — this simulates an
    # unexpected exception in a path that didn't have its own audit.
    monkeypatch.setattr(policy_mod, "_categorize_skip", boom_categorize)

    # Disable policy so matches_tier_1 returns False and we reach
    # _categorize_skip(reason).
    policy_mod.set_policy(False)

    with pytest.raises(RuntimeError, match="simulated upstream code bug"):
        await maybe_auto_submit("chain_audit_unexpected_1")

    row = await db[SHARED_GATE_RESULTS].find_one(
        {"intent_id": "chain_audit_unexpected_1", "kind": "auto_submit_exception"},
    )
    assert row is not None, "expected outer wrapper to write auto_submit_exception"
    assert row["skip_category"] == "internal_error"
    assert "simulated upstream code bug" in row["reason"]
