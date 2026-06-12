"""Test for the submit-block audit fallback endpoint (P1, 2026-02-19).

Operator reported: clicking submit on a `dry_run_passed` intent on
production returned `HTTP 403` with NO structured detail — the prod
proxy was stripping the response body on 4xx. Backend was returning
the rich `{blocked_by, reason, gates}` payload, but the body never
reached the browser.

Fix: expose `/execution/last-submit-block?intent_id=...` that re-reads
the audit row written to `shared_gate_results` on every block. The UI
fetches this as a fallback when the inline 403 body is opaque,
recovering the structured detail.

These tests pin:
  * 404 when no submit_block audit row exists.
  * 200 with synthesized `blocked_by`/`reason` from the first failing
    gate when an audit row exists.
  * Returns the FULL gates array (not truncated), so the UI can render
    the same panel it would have shown inline.
  * Works for submit_timeout / submit_error rows too (so a Webull
    timeout produces an actionable surface).
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
import httpx

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from dotenv import load_dotenv  # noqa: E402
load_dotenv("/app/backend/.env")

from db import db  # noqa: E402
from namespaces import SHARED_GATE_RESULTS  # noqa: E402

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL") or os.environ.get("BACKEND_URL")
if not BASE_URL:
    # Local dev fallback — supervisor proxies /api → :8001.
    BASE_URL = "http://127.0.0.1:8001"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@pytest.fixture
def auth_token():
    """Log in once per test session."""
    with httpx.Client(base_url=BASE_URL, timeout=10) as c:
        r = c.post(
            "/api/auth/login",
            json={"email": "admin@risedual.io", "password": "risedual-admin-2026"},
        )
    if r.status_code != 200:
        pytest.skip(f"auth login failed ({r.status_code}); env not seeded")
    body = r.json()
    tok = body.get("token") or body.get("access_token")
    if not tok:
        pytest.skip("auth response missing token")
    return tok


@pytest.fixture
def isolated_intent_id():
    iid = f"submit-block-test-{uuid.uuid4().hex[:10]}"
    yield iid
    async def _cleanup():
        await db[SHARED_GATE_RESULTS].delete_many({"intent_id": iid})
    asyncio.get_event_loop().run_until_complete(_cleanup())


def test_returns_404_when_no_audit_row(auth_token, isolated_intent_id):
    with httpx.Client(base_url=BASE_URL, timeout=10) as c:
        r = c.get(
            f"/api/execution/last-submit-block?intent_id={isolated_intent_id}",
            headers={"Authorization": f"Bearer {auth_token}"},
        )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_returns_first_failing_gate(auth_token, isolated_intent_id):
    # Seed a submit_blocked audit row mirroring what the live submit
    # endpoint writes when a block fires.
    gates = [
        {"name": "schema_invariants", "passed": True, "reason": "OK"},
        {"name": "lane_execution_enabled", "passed": True, "reason": "OK"},
        {"name": "cap_per_order",
         "passed": False,
         "reason": "WEBULL_NOTIONAL_ABOVE_CAP — $15.00 > $10.00 for AAL"},
        {"name": "broker_connected", "passed": True, "reason": "OK"},
    ]
    await db[SHARED_GATE_RESULTS].insert_one({
        "intent_id": isolated_intent_id,
        "kind": "submit_blocked",
        "ts": _now_iso(),
        "by": "admin@risedual.io",
        "order_notional_usd": 15.0,
        "verdict": "would_block",
        "gates": gates,
    })

    with httpx.Client(base_url=BASE_URL, timeout=10) as c:
        r = c.get(
            f"/api/execution/last-submit-block?intent_id={isolated_intent_id}",
            headers={"Authorization": f"Bearer {auth_token}"},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["intent_id"] == isolated_intent_id
    assert body["kind"] == "submit_blocked"
    assert body["blocked_by"] == "cap_per_order"
    assert "WEBULL_NOTIONAL_ABOVE_CAP" in body["reason"]
    # Full gates array must be present for the UI's failing-gates panel.
    assert len(body["gates"]) == 4
    assert any(g["name"] == "schema_invariants" for g in body["gates"])
    assert body["_from_audit"] is True


@pytest.mark.asyncio
async def test_handles_submit_timeout_row(auth_token, isolated_intent_id):
    """A broker_submit_timeout writes a row with `kind=submit_timeout`
    and `reason` instead of `gates`. The endpoint must still produce
    a sensible `blocked_by` + `reason` for the UI."""
    await db[SHARED_GATE_RESULTS].insert_one({
        "intent_id": isolated_intent_id,
        "kind": "submit_timeout",
        "ts": _now_iso(),
        "by": "admin@risedual.io",
        "reason": "broker_submit_timeout_20s",
    })
    with httpx.Client(base_url=BASE_URL, timeout=10) as c:
        r = c.get(
            f"/api/execution/last-submit-block?intent_id={isolated_intent_id}",
            headers={"Authorization": f"Bearer {auth_token}"},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "submit_timeout"
    assert body["blocked_by"] == "submit_timeout"
    assert body["reason"] == "broker_submit_timeout_20s"


@pytest.mark.asyncio
async def test_returns_most_recent_when_multiple(auth_token, isolated_intent_id):
    """If the operator clicked submit twice and got blocked twice, the
    endpoint must return the LATEST attempt — not a stale one."""
    older = datetime(2026, 1, 1, tzinfo=timezone.utc).isoformat()
    newer = datetime(2026, 6, 1, tzinfo=timezone.utc).isoformat()
    await db[SHARED_GATE_RESULTS].insert_many([
        {
            "intent_id": isolated_intent_id, "kind": "submit_blocked",
            "ts": older, "by": "admin@risedual.io",
            "gates": [{"name": "old_gate", "passed": False, "reason": "old"}],
        },
        {
            "intent_id": isolated_intent_id, "kind": "submit_blocked",
            "ts": newer, "by": "admin@risedual.io",
            "gates": [{"name": "new_gate", "passed": False, "reason": "new"}],
        },
    ])
    with httpx.Client(base_url=BASE_URL, timeout=10) as c:
        r = c.get(
            f"/api/execution/last-submit-block?intent_id={isolated_intent_id}",
            headers={"Authorization": f"Bearer {auth_token}"},
        )
    assert r.status_code == 200
    assert r.json()["blocked_by"] == "new_gate"
    assert r.json()["reason"] == "new"


def test_requires_auth():
    with httpx.Client(base_url=BASE_URL, timeout=10) as c:
        r = c.get("/api/execution/last-submit-block?intent_id=anything")
    assert r.status_code in (401, 403)
