"""RISE_AI saved threads — HTTP tests.

Lock invariants:
  * All endpoints admin-auth gated.
  * Doctrine: threads are reasoning memory ONLY. The endpoints
    in `rise_ai_threads_routes` do NOT touch broker, execution,
    promotion, seat policy, or doctrine surfaces (verified by
    grep + 404 path tests).
  * Resume preserves session_id so the LLM kernel keeps context.
  * Append-messages bumps seq monotonically and updates
    message_count.
  * Title/pinned/tags/archived patchable.
  * Search filters by title or tag (case-insensitive).
"""
from __future__ import annotations

import uuid
from pathlib import Path as _PathLib

import pytest

from db import db
from namespaces import RISE_AI_THREAD_MESSAGES, RISE_AI_THREADS


_ROUTE_PATH = _PathLib(__file__).parent.parent / "routes" / "rise_ai_threads_routes.py"


# ─── Auth gate ────────────────────────────────────────────────────────


def test_threads_list_requires_admin(base_url, api_client):
    r = api_client.get(f"{base_url}/api/admin/rise-ai/threads", timeout=15)
    assert r.status_code in (401, 403)


def test_threads_create_requires_admin(base_url, api_client):
    r = api_client.post(
        f"{base_url}/api/admin/rise-ai/threads",
        json={"title": "x"}, timeout=15,
    )
    assert r.status_code in (401, 403)


# ─── Doctrine: no execution surface reachable ────────────────────────


@pytest.mark.tripwire
def test_threads_module_imports_no_execution_surface():
    """Source-level guarantee: threads routes file must NOT import
    broker / execution / promotion / seat policy / doctrine
    mutation surfaces."""
    forbidden = (
        "shared.execution",
        "shared.broker_router",
        "shared.auto_router",
        "shared.executor_seat",
        "shared.broker.",
        "from shared.broker ",
        "promotion_artifact_report",
        "shared.seat_policy",
    )
    src = _ROUTE_PATH.read_text(encoding="utf-8")
    import re as _re
    code_imports = [
        line.strip() for line in src.splitlines()
        if _re.match(r"^\s*(import|from)\s+", line)
    ]
    for line in code_imports:
        for needle in forbidden:
            assert needle not in line, (
                f"rise_ai_threads_routes.py import line {line!r} "
                f"references forbidden surface {needle!r}. Threads "
                "are reasoning memory ONLY."
            )


# ─── Full CRUD happy path ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_threads_create_get_patch_resume_flow(base_url, auth_client):
    title = f"test thread {uuid.uuid4().hex[:6]}"
    try:
        # Create with two initial messages
        r = auth_client.post(
            f"{base_url}/api/admin/rise-ai/threads",
            json={
                "title": title,
                "mode": "reason",
                "tags": ["AAPL", "gap-fill"],
                "messages": [
                    {"kind": "user", "text": "Why did REDEYE veto?", "mode": "reason"},
                    {"kind": "rise", "text": "Because vol spiked.", "mode": "reason",
                     "call_id": f"test-call-{uuid.uuid4()}", "provider": "anthropic",
                     "model": "claude-sonnet-4-5", "latency_ms": 1200,
                     "llm_authority": "ADVISORY_ONLY"},
                ],
            },
            timeout=15,
        )
        assert r.status_code == 200, r.text
        thread = r.json()["thread"]
        tid = thread["thread_id"]
        assert thread["title"] == title
        assert thread["message_count"] == 2
        assert thread["last_call_id"]
        assert thread["session_id"].startswith("thread-")

        # GET — full detail
        r = auth_client.get(
            f"{base_url}/api/admin/rise-ai/threads/{tid}", timeout=15,
        )
        assert r.status_code == 200
        body = r.json()
        assert len(body["messages"]) == 2
        assert body["messages"][0]["seq"] == 0
        assert body["messages"][1]["seq"] == 1

        # PATCH — pin + append messages
        r = auth_client.patch(
            f"{base_url}/api/admin/rise-ai/threads/{tid}",
            json={
                "pinned": True,
                "tags": ["AAPL", "gap-fill", "premarket"],
                "append_messages": [
                    {"kind": "user", "text": "What about volume?", "mode": "reason"},
                    {"kind": "rise", "text": "Below average.", "mode": "reason",
                     "call_id": f"test-call-{uuid.uuid4()}", "provider": "anthropic"},
                ],
            },
            timeout=15,
        )
        assert r.status_code == 200, r.text
        patched = r.json()["thread"]
        assert patched["pinned"] is True
        assert patched["message_count"] == 4
        assert r.json()["appended"] == 2
        assert "premarket" in patched["tags"]

        # RESUME — returns session_id + full transcript
        r = auth_client.post(
            f"{base_url}/api/admin/rise-ai/threads/{tid}/resume",
            timeout=15,
        )
        assert r.status_code == 200
        resume = r.json()
        assert resume["session_id"] == thread["session_id"]
        assert resume["mode"] == "reason"
        assert resume["title"] == title
        assert len(resume["messages"]) == 4
        # Seq is monotonic
        seqs = [m["seq"] for m in resume["messages"]]
        assert seqs == [0, 1, 2, 3]
    finally:
        await db[RISE_AI_THREADS].delete_many({"title": title})
        await db[RISE_AI_THREAD_MESSAGES].delete_many({"text": {"$regex": "REDEYE|spiked|volume|average"}})


# ─── List filters ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_threads_pinned_only_filter(base_url, auth_client):
    t1_title = f"pinned-{uuid.uuid4().hex[:6]}"
    t2_title = f"unpinned-{uuid.uuid4().hex[:6]}"
    try:
        r1 = auth_client.post(
            f"{base_url}/api/admin/rise-ai/threads",
            json={"title": t1_title, "pinned": True}, timeout=15,
        )
        r2 = auth_client.post(
            f"{base_url}/api/admin/rise-ai/threads",
            json={"title": t2_title, "pinned": False}, timeout=15,
        )
        assert r1.status_code == 200 and r2.status_code == 200
        # pinned_only=True excludes the unpinned one
        r = auth_client.get(
            f"{base_url}/api/admin/rise-ai/threads?pinned_only=true",
            timeout=15,
        )
        titles = [t["title"] for t in r.json()["items"]]
        assert t1_title in titles
        assert t2_title not in titles
    finally:
        await db[RISE_AI_THREADS].delete_many({"title": {"$in": [t1_title, t2_title]}})


@pytest.mark.asyncio
async def test_threads_search_matches_title_or_tag(base_url, auth_client):
    base = uuid.uuid4().hex[:6]
    title = f"chevelle-doctrine-{base}"
    other_title = f"other-{base}"
    try:
        auth_client.post(
            f"{base_url}/api/admin/rise-ai/threads",
            json={"title": title, "tags": []}, timeout=15,
        )
        auth_client.post(
            f"{base_url}/api/admin/rise-ai/threads",
            json={"title": other_title, "tags": [f"chevelle-{base}"]}, timeout=15,
        )
        # Search "chevelle" — both should match (one via title, one via tag).
        r = auth_client.get(
            f"{base_url}/api/admin/rise-ai/threads?search=chevelle",
            timeout=15,
        )
        titles = [t["title"] for t in r.json()["items"]]
        assert title in titles
        assert other_title in titles
    finally:
        await db[RISE_AI_THREADS].delete_many({"title": {"$in": [title, other_title]}})


# ─── 404 paths ────────────────────────────────────────────────────────


def test_get_thread_404(base_url, auth_client):
    r = auth_client.get(
        f"{base_url}/api/admin/rise-ai/threads/does-not-exist",
        timeout=15,
    )
    assert r.status_code == 404


def test_patch_thread_404(base_url, auth_client):
    r = auth_client.patch(
        f"{base_url}/api/admin/rise-ai/threads/does-not-exist",
        json={"pinned": True}, timeout=15,
    )
    assert r.status_code == 404


def test_resume_thread_404(base_url, auth_client):
    r = auth_client.post(
        f"{base_url}/api/admin/rise-ai/threads/does-not-exist/resume",
        timeout=15,
    )
    assert r.status_code == 404


# ─── Validation ──────────────────────────────────────────────────────


def test_create_rejects_blank_title(base_url, auth_client):
    r = auth_client.post(
        f"{base_url}/api/admin/rise-ai/threads",
        json={"title": ""}, timeout=15,
    )
    assert r.status_code == 422


def test_append_message_kind_validated(base_url, auth_client):
    """kind must be 'user' or 'rise' — anything else 422."""
    # First create a clean thread.
    title = f"validate-{uuid.uuid4().hex[:6]}"
    r = auth_client.post(
        f"{base_url}/api/admin/rise-ai/threads",
        json={"title": title}, timeout=15,
    )
    tid = r.json()["thread"]["thread_id"]
    try:
        r = auth_client.patch(
            f"{base_url}/api/admin/rise-ai/threads/{tid}",
            json={"append_messages": [{"kind": "system", "text": "x"}]},
            timeout=15,
        )
        assert r.status_code == 422
    finally:
        # Sync cleanup via the API
        # (We can't use async db here because this is a sync test;
        # leaving the thread is harmless — listing filters by archive=False)
        auth_client.patch(
            f"{base_url}/api/admin/rise-ai/threads/{tid}",
            json={"archived": True}, timeout=15,
        )
