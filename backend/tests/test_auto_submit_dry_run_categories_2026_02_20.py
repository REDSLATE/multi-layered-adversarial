"""2026-02-20 — dry-run skip category split regression.

The legacy `_categorize_skip` collapsed three distinct dry-run states
(blocked / pending / missing) into a single `dry_run_not_ready`
bucket, which made the post-mortem panel useless for diagnosing why
an intent wasn't auto-submitted. These tests pin the split so a
future refactor can't silently re-collapse it.

Operator-visible doctrine encoded here:
  * dry_run_blocked → dry-run ran, refused. Working as designed.
  * dry_run_pending → dry-run task still running. Benign race.
  * dry_run_missing → dry_run_state never set. SILENT LEAK.
  * dry_run_not_ready (legacy bucket) → only when the literal state
    is some new/unknown value we haven't catalogued yet.
"""
from __future__ import annotations

import pytest

from shared.auto_submit_policy import (
    SKIP_CATEGORY_DRY_RUN_BLOCKED,
    SKIP_CATEGORY_DRY_RUN_MISSING,
    SKIP_CATEGORY_DRY_RUN_NOT_READY,
    SKIP_CATEGORY_DRY_RUN_PENDING,
    _categorize_skip,
    matches_tier_1,
)


# ── Direct mapper checks ─────────────────────────────────────────────
def test_categorize_dry_run_blocked_state():
    assert (
        _categorize_skip("dry_run_state 'blocked' != required 'passed'")
        == SKIP_CATEGORY_DRY_RUN_BLOCKED
    )


def test_categorize_dry_run_blocked_long_state_literal():
    # `gate_state` field uses `dry_run_blocked` as the literal in some
    # code paths — make sure that flavour also lands in the blocked
    # bucket rather than falling back to the legacy one.
    assert (
        _categorize_skip("dry_run_state 'dry_run_blocked' != required 'passed'")
        == SKIP_CATEGORY_DRY_RUN_BLOCKED
    )


def test_categorize_dry_run_failed_aliases():
    for literal in ("fail", "failed", "rejected_at_ingest"):
        assert (
            _categorize_skip(f"dry_run_state '{literal}' != required 'passed'")
            == SKIP_CATEGORY_DRY_RUN_BLOCKED
        ), literal


def test_categorize_dry_run_pending_state():
    assert (
        _categorize_skip("dry_run_state 'pending' != required 'passed'")
        == SKIP_CATEGORY_DRY_RUN_PENDING
    )


def test_categorize_dry_run_pending_aliases():
    for literal in ("running", "queued", "dry_run_pending"):
        assert (
            _categorize_skip(f"dry_run_state '{literal}' != required 'passed'")
            == SKIP_CATEGORY_DRY_RUN_PENDING
        ), literal


def test_categorize_dry_run_missing_empty_state():
    # Most common silent-leak flavour: intent ingested but
    # dry_run_state field never set.
    assert (
        _categorize_skip("dry_run_state '' != required 'passed'")
        == SKIP_CATEGORY_DRY_RUN_MISSING
    )


def test_categorize_dry_run_missing_unknown_state():
    assert (
        _categorize_skip("dry_run_state 'unknown' != required 'passed'")
        == SKIP_CATEGORY_DRY_RUN_MISSING
    )


def test_categorize_dry_run_unknown_state_falls_back_to_legacy_bucket():
    # A brand-new literal we haven't catalogued. Don't misclassify
    # into one of the three actionable buckets — punt to the legacy
    # `not_ready` bucket so the operator sees it as "unknown skip"
    # rather than "silent leak".
    assert (
        _categorize_skip("dry_run_state 'frobnicating' != required 'passed'")
        == SKIP_CATEGORY_DRY_RUN_NOT_READY
    )


# ── End-to-end via matches_tier_1 ────────────────────────────────────
BASE_POLICY = {
    "enabled": True,
    "allowed_actions": ["BUY", "SELL"],
    "allowed_lanes": ["equity", "crypto"],
    "allowed_brains": ["camino"],
    "confidence_min": 0.5,
    "required_dry_run_state": "passed",
    "tier_name": "tier_1_conservative",
}


def _intent(dry_run_state):
    return {
        "action": "BUY",
        "lane": "crypto",  # crypto = no market-hours gate dependency
        "stack": "camino",
        "confidence": 0.9,
        "dry_run_state": dry_run_state,
    }


@pytest.mark.asyncio
async def test_e2e_blocked_lands_in_blocked_bucket():
    ok, reason = await matches_tier_1(_intent("blocked"), BASE_POLICY)
    assert ok is False
    assert _categorize_skip(reason) == SKIP_CATEGORY_DRY_RUN_BLOCKED


@pytest.mark.asyncio
async def test_e2e_pending_lands_in_pending_bucket():
    ok, reason = await matches_tier_1(_intent("pending"), BASE_POLICY)
    assert ok is False
    assert _categorize_skip(reason) == SKIP_CATEGORY_DRY_RUN_PENDING


@pytest.mark.asyncio
async def test_e2e_missing_lands_in_missing_bucket():
    ok, reason = await matches_tier_1(_intent(""), BASE_POLICY)
    assert ok is False
    assert _categorize_skip(reason) == SKIP_CATEGORY_DRY_RUN_MISSING


@pytest.mark.asyncio
async def test_e2e_missing_when_field_absent():
    intent = _intent("")
    intent.pop("dry_run_state")
    ok, reason = await matches_tier_1(intent, BASE_POLICY)
    assert ok is False
    assert _categorize_skip(reason) == SKIP_CATEGORY_DRY_RUN_MISSING
