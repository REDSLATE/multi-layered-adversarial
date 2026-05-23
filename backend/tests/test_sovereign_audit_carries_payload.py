"""Tripwire — sovereign audit log carries full contribution content.

Doctrine pin (2026-05-23): the prior writer stamped only meta fields
(ts/brain/action/mode/training_signal/delta_was_clamped/posted_as/
seat_epoch) and threw away the brain's actual reasoning. Dashboards
read `payload: {}` and the operator couldn't tell whether a brain was
sending real content or empty heartbeats.

These tests pin the new shape so the audit row is never silently
gutted again.
"""
from __future__ import annotations

import pytest

from shared.sovereign_mode_guard import (
    SovereignContribution,
    _persist_snapshot,
    assert_contribution_safe,
)
from db import db


pytestmark = [pytest.mark.tripwire, pytest.mark.asyncio]


async def _post_and_fetch_audit(brain: str, contribution: SovereignContribution) -> dict:
    """Persist a contribution and return the resulting audit log row."""
    # Clean any prior test row so the assertion is deterministic.
    await db.sovereign_audit_log.delete_many({"brain": brain, "notes": contribution.notes})
    guard = assert_contribution_safe(contribution)
    await _persist_snapshot(brain, contribution, guard)
    row = await db.sovereign_audit_log.find_one(
        {"brain": brain, "notes": contribution.notes},
        {"_id": 0},
        sort=[("ts", -1)],
    )
    return row


async def test_audit_row_carries_notes():
    """The brain's `notes` field is the human-readable reasoning. It
    MUST appear in the audit row — otherwise the operator has no way
    to see the dissent rationale."""
    c = SovereignContribution(
        mode="DTD",
        notes="tripwire-test: counter-momentum signal weak after VIX spike",
    )
    row = await _post_and_fetch_audit("redeye", c)
    assert row is not None
    assert row["notes"] == c.notes, (
        "sovereign_audit_log row MUST carry the contribution `notes` field. "
        "Without it, dashboards surface empty payloads and operators can't "
        "see what a brain actually argued."
    )


async def test_audit_row_carries_weights():
    """Weight vector is the brain's calibration state. It belongs in
    the audit log."""
    c = SovereignContribution(
        mode="DTD",
        weights={"feature_a": 0.5, "feature_b": -0.2},
        notes="tripwire-test-weights",
    )
    row = await _post_and_fetch_audit("redeye", c)
    assert row["weights"] == {"feature_a": 0.5, "feature_b": -0.2}


async def test_audit_row_has_substance_flag():
    """The `has_substance` flag lets dashboards filter heartbeat-only
    contributions from real ones in one query."""
    # Empty contribution — all defaults.
    empty = SovereignContribution(mode="DTD")
    row_empty = await _post_and_fetch_audit("redeye", empty)
    assert row_empty["has_substance"] is False

    # Substantive contribution.
    sub = SovereignContribution(
        mode="DTD",
        notes="tripwire-substance-true",
        weights={"x": 0.3},
    )
    row_sub = await _post_and_fetch_audit("redeye", sub)
    assert row_sub["has_substance"] is True


async def test_audit_row_carries_confidence_delta_pair():
    """Both clamped and raw confidence_delta must be visible so the
    operator can spot brains hammering against the cap."""
    c = SovereignContribution(
        mode="DTD",
        confidence_delta=0.8,   # will be clamped server-side
        delta_reason="tripwire-delta-test",
        notes="tripwire-delta-pair",
    )
    row = await _post_and_fetch_audit("redeye", c)
    assert "confidence_delta" in row
    assert "raw_confidence_delta" in row
    assert row["raw_confidence_delta"] == 0.8
    # confidence_delta should be the clamped value (≤ cap)
    assert row["confidence_delta"] <= row["raw_confidence_delta"]
    assert row["delta_reason"] == "tripwire-delta-test"


async def test_audit_row_carries_recent_outcomes_count():
    """`recent_outcomes_count` lets dashboards know "is this brain
    actually reporting trade outcomes?" without parsing the array."""
    c = SovereignContribution(
        mode="DTD",
        notes="tripwire-outcomes-count",
        recent_outcomes=[],
    )
    row = await _post_and_fetch_audit("redeye", c)
    assert row["recent_outcomes_count"] == 0
    assert row["recent_outcomes"] == []
