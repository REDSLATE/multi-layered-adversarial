"""Tripwire — sovereign contribution-health telemetry (2026-05-24).

Pins the canonical attempt-logging contract that lets MC's panel show
split counters per brain regardless of what the brain itself reports.

Invariants:
  1. Every successful 200 push writes one `pushed_200` row.
  2. Every 422 empty-payload rejection writes one `rejected_422` row.
  3. `X-Client-Request-Id` (when sent) is captured on both outcomes.
  4. The health endpoint returns one row per brain even when a brain
     has zero attempts (so the panel never renders ragged).
  5. Vocabulary aligns with brain-side counter names so cross-side
     panels read identically.
"""
from __future__ import annotations

import pytest

from db import db
from namespaces import SOVEREIGN_CONTRIB_ATTEMPTS
from shared.sovereign_mode_guard import (
    SovereignContribution,
    _log_contribution_attempt,
    contribution_health,
    post_sovereign_contribution,
)


pytestmark = [pytest.mark.tripwire, pytest.mark.asyncio]


@pytest.fixture
async def clean_attempts():
    """Wipe attempts before/after each test so counts are deterministic."""
    await db[SOVEREIGN_CONTRIB_ATTEMPTS].delete_many({})
    yield
    await db[SOVEREIGN_CONTRIB_ATTEMPTS].delete_many({})


async def test_log_attempt_persists_all_fields(clean_attempts):
    await _log_contribution_attempt(
        runtime="redeye",
        outcome="pushed_200",
        status_code=200,
        empty_fields=[],
        request_id="req-abc-123",
        error_kind=None,
    )
    row = await db[SOVEREIGN_CONTRIB_ATTEMPTS].find_one(
        {"brain": "redeye"}, {"_id": 0},
    )
    assert row is not None
    assert row["brain"] == "redeye"
    assert row["outcome"] == "pushed_200"
    assert row["status_code"] == 200
    assert row["request_id"] == "req-abc-123"
    assert row["empty_fields"] == []
    assert row["error_kind"] is None


async def test_endpoint_logs_pushed_200_with_request_id(
    clean_attempts, monkeypatch,
):
    """End-to-end: a successful POST writes one `pushed_200` row and
    echoes the request_id back in the response."""
    monkeypatch.setenv("RISEDUAL_REJECT_EMPTY_CONTRIBUTIONS", "true")
    monkeypatch.setenv("GTO_INGEST_TOKEN", "tripwire-token")

    result = await post_sovereign_contribution(
        body=SovereignContribution(mode="DTD", notes="real reasoning"),
        runtime="redeye",
        x_runtime_token="tripwire-token",
        x_client_request_id="req-tripwire-1",
    )
    assert isinstance(result, dict)
    assert result.get("request_id") == "req-tripwire-1"

    rows = await db[SOVEREIGN_CONTRIB_ATTEMPTS].find(
        {"brain": "redeye"}, {"_id": 0},
    ).to_list(10)
    assert len(rows) == 1
    assert rows[0]["outcome"] == "pushed_200"
    assert rows[0]["request_id"] == "req-tripwire-1"


async def test_endpoint_logs_rejected_422_with_empty_fields(
    clean_attempts, monkeypatch,
):
    """An empty payload triggers 422 AND writes a `rejected_422` row
    with the full empty_fields list, so the panel can show top offenders."""
    from fastapi import HTTPException

    monkeypatch.setenv("RISEDUAL_REJECT_EMPTY_CONTRIBUTIONS", "true")
    monkeypatch.setenv("GTO_INGEST_TOKEN", "tripwire-token")

    with pytest.raises(HTTPException) as exc:
        await post_sovereign_contribution(
            body=SovereignContribution(mode="DTD"),
            runtime="redeye",
            x_runtime_token="tripwire-token",
            x_client_request_id="req-tripwire-empty",
        )
    assert exc.value.status_code == 422
    assert exc.value.detail.get("request_id") == "req-tripwire-empty"

    rows = await db[SOVEREIGN_CONTRIB_ATTEMPTS].find(
        {"brain": "redeye"}, {"_id": 0},
    ).to_list(10)
    assert len(rows) == 1
    assert rows[0]["outcome"] == "rejected_422"
    assert rows[0]["status_code"] == 422
    assert rows[0]["error_kind"] == "empty_contribution"
    assert rows[0]["request_id"] == "req-tripwire-empty"
    assert set(rows[0]["empty_fields"]) == {
        "notes", "weights", "recent_outcomes",
        "delta_reason", "confidence_delta",
    }


async def test_health_endpoint_returns_one_row_per_brain(clean_attempts):
    """Even when zero attempts have been logged, the panel data
    structure is consistent — one row per brain with `health=no_data`."""
    result = await contribution_health(window=100, _user={"email": "test"})
    assert "brains" in result
    brains_in_response = {b["brain"] for b in result["brains"]}
    assert brains_in_response == {"alpha", "camaro", "chevelle", "redeye"}
    for b in result["brains"]:
        assert b["total_attempts"] == 0
        assert b["health"] == "no_data"


async def test_health_endpoint_splits_counters(clean_attempts):
    """A mix of pushed/rejected attempts produces split counters."""
    # Seed: 3 pushed + 1 rejected for redeye, 5 pushed for alpha.
    for _ in range(3):
        await _log_contribution_attempt(
            runtime="redeye", outcome="pushed_200", status_code=200,
            empty_fields=[], request_id=None, error_kind=None,
        )
    await _log_contribution_attempt(
        runtime="redeye", outcome="rejected_422", status_code=422,
        empty_fields=["notes", "weights", "recent_outcomes",
                      "delta_reason", "confidence_delta"],
        request_id=None, error_kind="empty_contribution",
    )
    for _ in range(5):
        await _log_contribution_attempt(
            runtime="alpha", outcome="pushed_200", status_code=200,
            empty_fields=[], request_id=None, error_kind=None,
        )

    result = await contribution_health(window=100, _user={"email": "test"})
    by_brain = {b["brain"]: b for b in result["brains"]}

    assert by_brain["redeye"]["total_attempts"] == 4
    assert by_brain["redeye"]["pushed_200"] == 3
    assert by_brain["redeye"]["rejected_422"] == 1
    assert by_brain["redeye"]["health"] in ("mostly_healthy", "degraded")
    # Top empty fields populated from the rejection.
    top_fields = {tf["field"] for tf in by_brain["redeye"]["top_empty_fields"]}
    assert "notes" in top_fields or "weights" in top_fields  # any of the 5

    assert by_brain["alpha"]["total_attempts"] == 5
    assert by_brain["alpha"]["pushed_200"] == 5
    assert by_brain["alpha"]["health"] == "healthy"

    assert by_brain["camaro"]["total_attempts"] == 0
    assert by_brain["chevelle"]["total_attempts"] == 0


async def test_health_endpoint_fighting_contract_verdict(clean_attempts):
    """When >=50% of attempts are 422s, the verdict is fighting_contract."""
    for _ in range(2):
        await _log_contribution_attempt(
            runtime="chevelle", outcome="pushed_200", status_code=200,
            empty_fields=[], request_id=None, error_kind=None,
        )
    for _ in range(8):
        await _log_contribution_attempt(
            runtime="chevelle", outcome="rejected_422", status_code=422,
            empty_fields=["notes"], request_id=None,
            error_kind="empty_contribution",
        )

    result = await contribution_health(window=100, _user={"email": "test"})
    chevelle = next(b for b in result["brains"] if b["brain"] == "chevelle")
    assert chevelle["health"] == "fighting_contract"
    assert chevelle["rejected_422"] == 8


async def test_health_window_caps_results(clean_attempts):
    """The window argument caps how many attempts feed the counts —
    older attempts roll off."""
    # Seed 50 pushed for alpha; ask for window=10.
    for _ in range(50):
        await _log_contribution_attempt(
            runtime="alpha", outcome="pushed_200", status_code=200,
            empty_fields=[], request_id=None, error_kind=None,
        )
    result = await contribution_health(window=10, _user={"email": "test"})
    alpha = next(b for b in result["brains"] if b["brain"] == "alpha")
    assert alpha["total_attempts"] == 10
    assert alpha["pushed_200"] == 10
