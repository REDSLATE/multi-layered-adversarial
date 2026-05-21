"""HTTP tests for `/api/admin/llm/ledger*` — the operator's
ledger view + grading surface.

Lock invariants:
  * Endpoints require admin auth.
  * Grading routes into preference_log + (for score>=1) the
    distillation queue. NEVER into execution.
  * Invalid scores rejected; unknown call_id 404s.
  * Grades visible on subsequent reads.
"""
from __future__ import annotations

import uuid

import pytest
import requests

from db import db
from namespaces import (
    LLM_CALLS,
    LLM_DISTILLATION_QUEUE,
    LLM_PREFERENCE_LOG,
)


def _seed_ledger_row(call_id: str, **overrides) -> dict:
    """Insert a synthetic llm_calls row so we can grade it."""
    from datetime import datetime, timezone
    doc = {
        "call_id": call_id,
        "session_id": f"sess-{call_id}",
        "role": "strategist",
        "task": "test_task",
        "provider": "anthropic",
        "model": "claude-sonnet-4-5-20250929",
        "ok": True,
        "error": None,
        "prompt": "test prompt content for ledger inspection",
        "response": "test response content for ledger inspection",
        "prompt_bytes": 40,
        "response_bytes": 42,
        "prompt_truncated": False,
        "response_truncated": False,
        "usage": {},
        "metadata": {"unit_test": True},
        "latency_ms": 123,
        "llm_authority": "ADVISORY_ONLY",
        "kernel_version": "0.2.0",
        "git_sha": "test",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    doc.update(overrides)
    return doc


# ─────────────── Auth gate ────────────────────────────────────────────


def test_ledger_requires_admin(base_url, api_client):
    r = api_client.get(f"{base_url}/api/admin/llm/ledger", timeout=15)
    assert r.status_code in (401, 403), r.text


def test_grade_requires_admin(base_url, api_client):
    r = api_client.post(
        f"{base_url}/api/admin/llm/ledger/any-id/grade",
        json={"score": 1, "outcome": "helpful"},
        timeout=15,
    )
    assert r.status_code in (401, 403), r.text


# ─────────────── List endpoint ────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_ledger_returns_recent_calls(base_url, auth_client):
    call_id = f"test-ledger-{uuid.uuid4()}"
    await db[LLM_CALLS].insert_one(_seed_ledger_row(call_id))
    try:
        r = auth_client.get(
            f"{base_url}/api/admin/llm/ledger?hours=1&limit=50",
            timeout=15,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        ids = [it["call_id"] for it in body["items"]]
        assert call_id in ids
        seeded = next(it for it in body["items"] if it["call_id"] == call_id)
        # Preview is short; full prompt fetched via detail endpoint.
        assert seeded["provider"] == "anthropic"
        assert seeded["grades_count"] == 0
        assert seeded["latest_grade"] is None
    finally:
        await db[LLM_CALLS].delete_many({"call_id": call_id})


@pytest.mark.asyncio
async def test_list_ledger_role_filter(base_url, auth_client):
    cid_a = f"test-strat-{uuid.uuid4()}"
    cid_b = f"test-gov-{uuid.uuid4()}"
    await db[LLM_CALLS].insert_one(_seed_ledger_row(cid_a, role="strategist"))
    await db[LLM_CALLS].insert_one(_seed_ledger_row(cid_b, role="governor"))
    try:
        r = auth_client.get(
            f"{base_url}/api/admin/llm/ledger?hours=1&role=governor&limit=50",
            timeout=15,
        )
        assert r.status_code == 200, r.text
        ids = {it["call_id"] for it in r.json()["items"]}
        assert cid_b in ids
        assert cid_a not in ids
    finally:
        await db[LLM_CALLS].delete_many({"call_id": {"$in": [cid_a, cid_b]}})


# ─────────────── Detail endpoint ──────────────────────────────────────


@pytest.mark.asyncio
async def test_detail_returns_full_prompt_and_response(base_url, auth_client):
    call_id = f"test-detail-{uuid.uuid4()}"
    big_prompt = "X" * 500  # > PREVIEW_CHARS
    await db[LLM_CALLS].insert_one(
        _seed_ledger_row(call_id, prompt=big_prompt, response=big_prompt),
    )
    try:
        r = auth_client.get(
            f"{base_url}/api/admin/llm/ledger/{call_id}",
            timeout=15,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        # Detail must NOT truncate.
        assert body["call"]["prompt"] == big_prompt
        assert body["call"]["response"] == big_prompt
        assert body["grades"] == []
    finally:
        await db[LLM_CALLS].delete_many({"call_id": call_id})


def test_detail_404_on_unknown(base_url, auth_client):
    r = auth_client.get(
        f"{base_url}/api/admin/llm/ledger/does-not-exist",
        timeout=15,
    )
    assert r.status_code == 404


# ─────────────── Grading ──────────────────────────────────────────────


def test_grade_rejects_invalid_score(base_url, auth_client):
    r = auth_client.post(
        f"{base_url}/api/admin/llm/ledger/any-id/grade",
        json={"score": 99, "outcome": "wat"},
        timeout=15,
    )
    assert r.status_code == 400, r.text


def test_grade_404_on_unknown_call(base_url, auth_client):
    r = auth_client.post(
        f"{base_url}/api/admin/llm/ledger/missing/grade",
        json={"score": 1, "outcome": "helpful"},
        timeout=15,
    )
    assert r.status_code == 404, r.text


@pytest.mark.asyncio
async def test_grade_writes_preference_and_does_not_enqueue_zero(base_url, auth_client):
    call_id = f"test-grade-zero-{uuid.uuid4()}"
    await db[LLM_CALLS].insert_one(_seed_ledger_row(call_id))
    try:
        r = auth_client.post(
            f"{base_url}/api/admin/llm/ledger/{call_id}/grade",
            json={"score": 0, "outcome": "neutral"},
            timeout=15,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["enqueued_for_distillation"] is False
        # Preference row exists.
        pref = await db[LLM_PREFERENCE_LOG].find_one({"call_id": call_id})
        assert pref is not None
        # Distillation row does NOT.
        dist = await db[LLM_DISTILLATION_QUEUE].find_one({"call_id": call_id})
        assert dist is None
    finally:
        await db[LLM_CALLS].delete_many({"call_id": call_id})
        await db[LLM_PREFERENCE_LOG].delete_many({"call_id": call_id})


@pytest.mark.asyncio
async def test_grade_positive_enqueues_for_distillation(base_url, auth_client):
    call_id = f"test-grade-win-{uuid.uuid4()}"
    await db[LLM_CALLS].insert_one(_seed_ledger_row(call_id))
    try:
        r = auth_client.post(
            f"{base_url}/api/admin/llm/ledger/{call_id}/grade",
            json={"score": 1, "outcome": "helpful", "note": "good call"},
            timeout=15,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["enqueued_for_distillation"] is True
        dist = await db[LLM_DISTILLATION_QUEUE].find_one({"call_id": call_id})
        assert dist is not None
        assert dist["score"] == 1
    finally:
        await db[LLM_CALLS].delete_many({"call_id": call_id})
        await db[LLM_PREFERENCE_LOG].delete_many({"call_id": call_id})
        await db[LLM_DISTILLATION_QUEUE].delete_many({"call_id": call_id})


@pytest.mark.asyncio
async def test_grades_visible_on_subsequent_reads(base_url, auth_client):
    call_id = f"test-grade-read-{uuid.uuid4()}"
    await db[LLM_CALLS].insert_one(_seed_ledger_row(call_id))
    try:
        auth_client.post(
            f"{base_url}/api/admin/llm/ledger/{call_id}/grade",
            json={"score": -1, "outcome": "wrong"},
            timeout=15,
        )
        # Detail shows it.
        r = auth_client.get(
            f"{base_url}/api/admin/llm/ledger/{call_id}",
            timeout=15,
        )
        body = r.json()
        assert len(body["grades"]) == 1
        assert body["grades"][0]["score"] == -1
        # List shows latest_grade.
        r2 = auth_client.get(
            f"{base_url}/api/admin/llm/ledger?hours=1&limit=200",
            timeout=15,
        )
        seeded = next(
            it for it in r2.json()["items"] if it["call_id"] == call_id
        )
        assert seeded["grades_count"] == 1
        assert seeded["latest_grade"]["score"] == -1
    finally:
        await db[LLM_CALLS].delete_many({"call_id": call_id})
        await db[LLM_PREFERENCE_LOG].delete_many({"call_id": call_id})
        await db[LLM_DISTILLATION_QUEUE].delete_many({"call_id": call_id})


# ─────────────── Doctrine: ADVISORY_ONLY passthrough ──────────────────


@pytest.mark.tripwire
@pytest.mark.asyncio
async def test_ledger_rows_carry_advisory_only_stamp(base_url, auth_client):
    """Every llm_calls row written by the kernel carries
    `llm_authority: ADVISORY_ONLY`. The ledger endpoint must not
    strip or mutate that field."""
    call_id = f"test-stamp-{uuid.uuid4()}"
    await db[LLM_CALLS].insert_one(_seed_ledger_row(call_id))
    try:
        r = auth_client.get(
            f"{base_url}/api/admin/llm/ledger/{call_id}", timeout=15,
        )
        assert r.status_code == 200
        assert r.json()["call"]["llm_authority"] == "ADVISORY_ONLY"
    finally:
        await db[LLM_CALLS].delete_many({"call_id": call_id})
