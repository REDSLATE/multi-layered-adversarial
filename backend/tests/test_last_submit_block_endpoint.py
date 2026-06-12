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

import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
import httpx
from pymongo import MongoClient

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from dotenv import load_dotenv  # noqa: E402
load_dotenv("/app/backend/.env")

from namespaces import SHARED_GATE_RESULTS  # noqa: E402

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL") or os.environ.get("BACKEND_URL")
if not BASE_URL:
    # Local dev fallback — supervisor proxies /api → :8001.
    BASE_URL = "http://127.0.0.1:8001"

# Sync pymongo client so seed/cleanup doesn't fight pytest-asyncio's
# event-loop lifecycle. Motor's `db` is bound to the app's running
# loop, which conflicts with the per-test loop pytest-asyncio creates.
_MONGO = MongoClient(os.environ["MONGO_URL"])
_DB_NAME = os.environ.get("DB_NAME", "test_database")
_sync_db = _MONGO[_DB_NAME]


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
    _sync_db[SHARED_GATE_RESULTS].delete_many({"intent_id": iid})


def test_returns_404_when_no_audit_row(auth_token, isolated_intent_id):
    with httpx.Client(base_url=BASE_URL, timeout=10) as c:
        r = c.get(
            f"/api/execution/last-submit-block?intent_id={isolated_intent_id}",
            headers={"Authorization": f"Bearer {auth_token}"},
        )
    assert r.status_code == 404


def test_returns_first_failing_gate(auth_token, isolated_intent_id):
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
    _sync_db[SHARED_GATE_RESULTS].insert_one({
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


def test_handles_submit_timeout_row(auth_token, isolated_intent_id):
    """A broker_submit_timeout writes a row with `kind=submit_timeout`
    and `reason` instead of `gates`. The endpoint must still produce
    a sensible `blocked_by` + `reason` for the UI, AND synthesize a
    one-row `gates` array so the UI's failing-gates panel has
    something to render."""
    _sync_db[SHARED_GATE_RESULTS].insert_one({
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
    # Synthetic gate name surfaces as blocked_by.
    assert body["blocked_by"] == "broker_submit_timeout"
    assert body["reason"] == "broker_submit_timeout_20s"
    # Synthetic single-row gates array so the UI panel has content.
    assert len(body["gates"]) == 1
    assert body["gates"][0]["passed"] is False


def test_handles_submit_no_trade_row(auth_token, isolated_intent_id):
    """The MOST COMMON 403 source on the small-pilot route:
    `BrokerRouteBlocked` from broker_router writes a row with
    `kind=submit_no_trade` and `reason` — NOT a `gates` array.

    Before the 2026-02-19 (rev2) fix, the fallback's `kind` filter
    didn't include `submit_no_trade`, so the endpoint returned 404
    and the UI red bar stayed blank when the prod proxy stripped
    the 403 body. This test pins the regression — the endpoint MUST
    find the row and synthesize a readable `blocked_by` + `reason`."""
    _sync_db[SHARED_GATE_RESULTS].insert_one({
        "intent_id": isolated_intent_id,
        "kind": "submit_no_trade",
        "ts": _now_iso(),
        "by": "admin@risedual.io",
        "reason": "MC receipt rejected: seat_self_review_block; NO_TRADE",
    })
    with httpx.Client(base_url=BASE_URL, timeout=10) as c:
        r = c.get(
            f"/api/execution/last-submit-block?intent_id={isolated_intent_id}",
            headers={"Authorization": f"Bearer {auth_token}"},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "submit_no_trade"
    assert body["blocked_by"] == "broker_router"
    assert "seat_self_review_block" in body["reason"]
    # UI must have a non-empty gates list to render the failing-gate
    # panel — the fix synthesizes a virtual gate when the kind isn't
    # from the schema-gate chain.
    assert len(body["gates"]) == 1
    assert body["gates"][0]["name"] == "broker_router"
    assert body["gates"][0]["passed"] is False


def test_handles_submit_error_row(auth_token, isolated_intent_id):
    """`submit_error` rows store an `error` field (not `reason`).
    The endpoint must still surface a meaningful detail."""
    _sync_db[SHARED_GATE_RESULTS].insert_one({
        "intent_id": isolated_intent_id,
        "kind": "submit_error",
        "ts": _now_iso(),
        "by": "admin@risedual.io",
        "error": "WebullSDKError: connection_reset",
    })
    with httpx.Client(base_url=BASE_URL, timeout=10) as c:
        r = c.get(
            f"/api/execution/last-submit-block?intent_id={isolated_intent_id}",
            headers={"Authorization": f"Bearer {auth_token}"},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "submit_error"
    assert body["blocked_by"] == "broker_submit_error"
    assert "WebullSDKError" in body["reason"]


def test_returns_most_recent_when_multiple(auth_token, isolated_intent_id):
    """If the operator clicked submit twice and got blocked twice, the
    endpoint must return the LATEST attempt — not a stale one."""
    older = datetime(2026, 1, 1, tzinfo=timezone.utc).isoformat()
    newer = datetime(2026, 6, 1, tzinfo=timezone.utc).isoformat()
    _sync_db[SHARED_GATE_RESULTS].insert_many([
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
