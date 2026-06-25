"""Seat-authority pre-check in `matches_tier_1` — 2026-02-23.

Background: the three-mode execution-authority doctrine
(`seat_bound` / `requires_override` / `vacant`) is enforced in
`shared/execution.py:_evaluate_gates` via the
`seat_authority_classification` block, and `execution_submit`
refuses with HTTP 403 when `requires_operator_override=True` and
the caller did not explicitly opt in.

Before this fix, the auto-submit chain (`maybe_auto_submit`) would
call `execution_submit` for EVERY tier-1 match — including non-
seat-holder intents — letting the 403 raise out of the submit
call, caught by the chain's exception handler, and filed under
`auto_submit_failed/submit_raised`. On prod 2026-06-25 this
produced 422 doctrine-correct refusals labeled as pipeline
failures on the operator's post-mortem panel.

The fix mirrors the 3-mode classification inside `matches_tier_1`
itself: when the intent's emitting brain (`stack`) is not the
current seat holder for the intent's lane, return a clean
`(False, "seat_authority intent author …")` so `maybe_auto_submit`
writes a `auto_submit_skipped` row with
`skip_category=seat_authority_mismatch`. The post-mortem panel
then surfaces these as "Skipped by Shelly · brain ≠ seat holder
(doctrine OK)" instead of "submit_raised".

The downstream `_evaluate_gates` enforcement is unchanged — this
is just a clean SKIP mirror so the audit trail tells the truth.
"""
from __future__ import annotations

import pytest

from shared.auto_submit_policy import (
    matches_tier_1,
    reset_policy_for_tests,
    set_policy,
    _categorize_skip,
    SKIP_CATEGORY_SEAT_AUTHORITY_MISMATCH,
    SKIP_CATEGORY_SEAT_VACANT,
)


pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _reset_policy_and_bypass_market_hours(monkeypatch):
    """Reset Shelly's policy + bypass the equity market-hours gate so
    these tests are clock-independent. They're about the seat-
    authority pre-check, not RTH enforcement."""
    monkeypatch.setenv("RISEDUAL_BYPASS_MARKET_HOURS", "true")
    reset_policy_for_tests()
    yield
    reset_policy_for_tests()


def _patch_seat(monkeypatch, *, holder):
    """Stub the seat lookup helpers used by `matches_tier_1`.

    `holder` may be the canonical brain_id (camino/barracuda/hellcat/gto)
    OR None for the vacant case. We stub at the module the
    auto-submit policy imports from — `shared.executor_seat`.
    """
    from shared import executor_seat as es
    from shared import seat_policy as sp

    async def fake_get_seat_holder(seat_name):  # noqa: ARG001
        return holder

    monkeypatch.setattr(es, "get_seat_holder", fake_get_seat_holder)
    monkeypatch.setattr(es, "seats_with_execute",
                        lambda lane: ["PASCHAR"] if lane else [])
    monkeypatch.setattr(sp, "seat_may_execute_lane",
                        lambda seat, lane: True)


async def test_passes_when_intent_author_matches_seat_holder(monkeypatch):
    """Camino emits, Camino holds the equity executor → matches_tier_1 OK."""
    set_policy(True)
    _patch_seat(monkeypatch, holder="camino")
    intent = {
        "action": "BUY", "lane": "equity", "stack": "camino",
        "confidence": 0.85, "dry_run_state": "passed",
    }
    ok, reason = await matches_tier_1(intent)
    assert ok is True, f"seat-bound camino intent should pass, got: {reason}"


async def test_skips_when_intent_author_mismatches_seat_holder(monkeypatch):
    """Camino emits, Barracuda holds the equity executor → clean skip.

    This is the EXACT case that was producing 422 `submit_raised` rows
    on the prod post-mortem panel. With the pre-check, it MUST surface
    as a `seat_authority_mismatch` skip.
    """
    set_policy(True)
    _patch_seat(monkeypatch, holder="barracuda")
    intent = {
        "action": "BUY", "lane": "equity", "stack": "camino",
        "confidence": 0.85, "dry_run_state": "passed",
    }
    ok, reason = await matches_tier_1(intent)
    assert ok is False
    assert reason.startswith("seat_authority "), reason
    assert "camino" in reason
    assert "barracuda" in reason
    assert "requires_override" in reason
    # Reason must map to the new skip category for the post-mortem panel.
    assert _categorize_skip(reason) == SKIP_CATEGORY_SEAT_AUTHORITY_MISMATCH


async def test_skips_when_no_seat_holder(monkeypatch):
    """No seat assigned → `seat_vacant` skip (operator-action label)."""
    set_policy(True)
    _patch_seat(monkeypatch, holder=None)
    intent = {
        "action": "BUY", "lane": "equity", "stack": "camino",
        "confidence": 0.85, "dry_run_state": "passed",
    }
    ok, reason = await matches_tier_1(intent)
    assert ok is False
    assert reason.startswith("seat_authority "), reason
    assert "vacant" in reason
    assert _categorize_skip(reason) == SKIP_CATEGORY_SEAT_VACANT


async def test_seat_check_runs_AFTER_cheap_filters(monkeypatch):
    """Confidence floor must still beat the seat check so we don't
    pay an unnecessary Mongo round-trip when the intent is going to
    skip on a cheaper reason anyway."""
    set_policy(True)
    # If the seat check ran first, this would raise (stub not set)
    # — proving the cheap filters short-circuit.
    intent = {
        "action": "BUY", "lane": "equity", "stack": "camino",
        "confidence": 0.10,  # below 0.70 default
        "dry_run_state": "passed",
    }
    ok, reason = await matches_tier_1(intent)
    assert ok is False
    assert "confidence" in reason


async def test_seat_check_defers_to_evaluate_gates_on_lookup_error(monkeypatch):
    """If the seat lookup itself raises (DB hiccup, transient), the
    pre-check must NOT block — `_evaluate_gates` is still the
    authoritative enforcement point downstream.
    """
    set_policy(True)
    from shared import executor_seat as es

    async def boom(seat_name):  # noqa: ARG001
        raise RuntimeError("simulated DB hiccup")

    monkeypatch.setattr(es, "get_seat_holder", boom)
    monkeypatch.setattr(es, "seats_with_execute",
                        lambda lane: ["PASCHAR"])
    intent = {
        "action": "BUY", "lane": "equity", "stack": "camino",
        "confidence": 0.85, "dry_run_state": "passed",
    }
    ok, reason = await matches_tier_1(intent)
    # Pre-check defers; intent is OK from policy's perspective.
    # The downstream `_evaluate_gates` will catch any actual doctrine
    # violation when execution_submit runs.
    assert ok is True, f"transient seat-lookup failure must not block: {reason}"
