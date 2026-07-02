"""Tripwire — empty sovereign contributions are rejected (2026-05-24).

Doctrine pin: heartbeat-style POSTs to the contribution endpoint were
generating skeleton rows in the audit log with no learning signal.
The endpoint now refuses them with HTTP 422 and a structured detail
listing which fields are empty.

Backward-compat: a contribution with AT LEAST ONE substantive field
(notes, weights, recent_outcomes, delta_reason, or non-zero
confidence_delta) is accepted. The threshold is "any substance," not
"all fields populated" — brains shouldn't be forced to invent values
they don't have.

Operator escape hatch: `RISEDUAL_REJECT_EMPTY_CONTRIBUTIONS=false`
disables enforcement (for the rare case where a brain genuinely needs
to send a hollow heartbeat during a ramp window).
"""
from __future__ import annotations

import os

import pytest

from shared.sovereign_mode_guard import (
    SovereignContribution,
    _list_empty_fields,
    _reject_empty_contributions_enabled,
)


pytestmark = pytest.mark.tripwire


def test_empty_contribution_lists_all_five_empty_fields():
    """The default SovereignContribution carries nothing — all five
    substance-check fields show up empty."""
    c = SovereignContribution(mode="DTD")
    empty = _list_empty_fields(c)
    assert set(empty) == {
        "notes", "weights", "recent_outcomes",
        "delta_reason", "confidence_delta",
    }
    assert len(empty) == 5


def test_contribution_with_notes_is_substantive():
    """A single non-empty `notes` field is sufficient to clear the
    substance gate. We don't force brains to fabricate weights or
    outcomes they don't have."""
    c = SovereignContribution(mode="DTD", notes="post-VIX-spike review")
    empty = _list_empty_fields(c)
    assert "notes" not in empty
    assert len(empty) < 5  # at least one field substantive


def test_contribution_with_outcomes_only_is_substantive():
    """recent_outcomes is the primary learning signal — a contribution
    that ships only outcomes (no commentary) is still substantive."""
    from shared.sovereign_mode_guard import SovereignOutcome
    c = SovereignContribution(
        mode="DTD",
        recent_outcomes=[
            SovereignOutcome(symbol="AAPL", action="BUY", confidence=0.7, outcome=1),
        ],
    )
    empty = _list_empty_fields(c)
    assert "recent_outcomes" not in empty


def test_contribution_with_weights_only_is_substantive():
    """Weights-only contribution (e.g., after retraining) is valid."""
    c = SovereignContribution(mode="DTD", weights={"trend": 0.3})
    empty = _list_empty_fields(c)
    assert "weights" not in empty


def test_whitespace_only_notes_counted_as_empty():
    """A `notes` field containing only whitespace shouldn't pass — that's
    the same as no notes."""
    c = SovereignContribution(mode="DTD", notes="   \t\n  ")
    empty = _list_empty_fields(c)
    assert "notes" in empty


def test_enforcement_flag_default_on(monkeypatch):
    """Doctrine pin: enforcement defaults to ON. Operator must explicitly
    opt out via `RISEDUAL_REJECT_EMPTY_CONTRIBUTIONS=false`."""
    monkeypatch.delenv("RISEDUAL_REJECT_EMPTY_CONTRIBUTIONS", raising=False)
    assert _reject_empty_contributions_enabled() is True


def test_enforcement_flag_explicit_false(monkeypatch):
    """Operators can opt out by setting the env var explicitly false."""
    for v in ("false", "False", "0", "no", "off"):
        monkeypatch.setenv("RISEDUAL_REJECT_EMPTY_CONTRIBUTIONS", v)
        assert _reject_empty_contributions_enabled() is False


@pytest.mark.asyncio
async def test_endpoint_rejects_empty_contribution(monkeypatch):
    """End-to-end: POST a fully-empty contribution and expect HTTP 422
    with a structured detail naming the empty fields."""
    from fastapi import HTTPException
    from shared.sovereign_mode_guard import post_sovereign_contribution

    # Ensure enforcement is on for this test.
    monkeypatch.setenv("RISEDUAL_REJECT_EMPTY_CONTRIBUTIONS", "true")
    # Provide a token so the auth check passes — we want to test the
    # substance check, not the auth check.
    monkeypatch.setenv("GTO_INGEST_TOKEN", "tripwire-test-token")

    empty = SovereignContribution(mode="DTD")
    with pytest.raises(HTTPException) as exc:
        await post_sovereign_contribution(
            body=empty,
            runtime="redeye",
            x_runtime_token="tripwire-test-token",
        )
    assert exc.value.status_code == 422
    assert isinstance(exc.value.detail, dict)
    assert exc.value.detail["error"] == "empty_contribution"
    assert set(exc.value.detail["empty_fields"]) == {
        "notes", "weights", "recent_outcomes",
        "delta_reason", "confidence_delta",
    }
    assert exc.value.detail["runtime"] == "redeye"


@pytest.mark.asyncio
async def test_endpoint_accepts_minimal_substantive_contribution(monkeypatch):
    """A contribution with ONLY `notes` populated is accepted (one
    substantive field is the threshold)."""
    from shared.sovereign_mode_guard import post_sovereign_contribution

    monkeypatch.setenv("RISEDUAL_REJECT_EMPTY_CONTRIBUTIONS", "true")
    monkeypatch.setenv("GTO_INGEST_TOKEN", "tripwire-test-token")

    minimal = SovereignContribution(mode="DTD", notes="tripwire-min-substance")
    # No exception — endpoint persists and returns. We don't assert on
    # the response shape (that's covered by other tests); just confirm
    # the substance gate let it through.
    result = await post_sovereign_contribution(
        body=minimal,
        runtime="redeye",
        x_runtime_token="tripwire-test-token",
    )
    assert result is not None


@pytest.mark.asyncio
async def test_endpoint_bypassed_when_flag_off(monkeypatch):
    """When the enforcement flag is OFF, empty contributions persist
    (back-compat with the pre-2026-05-24 behaviour). This is the rare
    ramp-window escape hatch."""
    from shared.sovereign_mode_guard import post_sovereign_contribution

    monkeypatch.setenv("RISEDUAL_REJECT_EMPTY_CONTRIBUTIONS", "false")
    monkeypatch.setenv("GTO_INGEST_TOKEN", "tripwire-test-token")

    empty = SovereignContribution(mode="DTD")
    # Should NOT raise — flag is off.
    result = await post_sovereign_contribution(
        body=empty,
        runtime="redeye",
        x_runtime_token="tripwire-test-token",
    )
    assert result is not None
