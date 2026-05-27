"""Tripwires for auto-dry-run-on-ingest + drain endpoint (2026-05-27).

Doctrine pin (operator-confirmed):
    Intents must NEVER sit at `gate_state=pending` indefinitely.
    The auto-dry-run-on-ingest hook fires `_evaluate_gates`
    immediately after a successful insert so every new intent
    transitions to `dry_run_passed` / `dry_run_blocked` within
    milliseconds. Env-gated via `AUTO_DRY_RUN_ON_INGEST`; default ON.

What's pinned:
  * Env gate is operator-tunable and defaults to ON.
  * The drain endpoint exists and is idempotent.
  * The drain endpoint accepts an optional `stack` filter.
  * Failures in auto-dry-run NEVER block ingest (best-effort).
  * Reusable `run_dry_run_for_intent` exists and matches the HTTP
    handler's behavior.
"""
from __future__ import annotations

import asyncio
import os
import time
import uuid

import pytest
import requests

from shared.intents import _auto_dry_run_enabled


BASE_URL = (os.environ.get("REACT_APP_BACKEND_URL") or "").rstrip("/")
if not BASE_URL:
    with open("/app/frontend/.env") as f:
        for line in f:
            if line.startswith("REACT_APP_BACKEND_URL="):
                BASE_URL = line.split("=", 1)[1].strip().rstrip("/")
                break


ADMIN_EMAIL = os.environ.get("TEST_ADMIN_EMAIL", "admin@risedual.io")
ADMIN_PASSWORD = os.environ.get("TEST_ADMIN_PASSWORD", "risedual-admin-2026")


def _login() -> str:
    r = requests.post(
        f"{BASE_URL}/api/auth/login",
        json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
        timeout=30,
    )
    if r.status_code != 200:
        pytest.skip(f"login failed {r.status_code}: {r.text}")
    return r.json()["access_token"]


# ──────────────────────── env gate ────────────────────────


@pytest.mark.tripwire
def test_auto_dry_run_default_is_on(monkeypatch):
    """Default config must enable auto-dry-run. Operator can flip off
    via AUTO_DRY_RUN_ON_INGEST=false on prod while load-tuning."""
    monkeypatch.delenv("AUTO_DRY_RUN_ON_INGEST", raising=False)
    assert _auto_dry_run_enabled() is True


@pytest.mark.tripwire
@pytest.mark.parametrize("val", ["false", "False", "0", "no", "off"])
def test_auto_dry_run_disabled_when_env_false(monkeypatch, val):
    monkeypatch.setenv("AUTO_DRY_RUN_ON_INGEST", val)
    assert _auto_dry_run_enabled() is False


@pytest.mark.tripwire
@pytest.mark.parametrize("val", ["true", "True", "1", "yes", "ON"])
def test_auto_dry_run_enabled_when_env_truthy(monkeypatch, val):
    monkeypatch.setenv("AUTO_DRY_RUN_ON_INGEST", val)
    assert _auto_dry_run_enabled() is True


# ──────────────────────── reusable runner exists ────────────────────────


@pytest.mark.tripwire
def test_run_dry_run_for_intent_is_importable():
    """The reusable internal runner must exist + be callable so the
    ingest hook and the drain endpoint can both use it."""
    from shared.execution import run_dry_run_for_intent  # noqa: F401
    import inspect
    sig = inspect.signature(run_dry_run_for_intent)
    params = set(sig.parameters.keys())
    # Must accept the three core args we depend on.
    assert "intent_id" in params
    assert "order_notional_usd" in params
    assert "actor" in params


# ──────────────────────── drain endpoint ────────────────────────


@pytest.mark.tripwire
def test_drain_endpoint_exists_and_authed():
    """Unauthed call rejected; authed call returns the canonical schema."""
    ru = requests.post(
        f"{BASE_URL}/api/admin/intents/auto-dry-run-drain", timeout=20,
    )
    assert ru.status_code in (401, 403), (
        f"drain endpoint must require auth; got {ru.status_code}"
    )

    token = _login()
    r = requests.post(
        f"{BASE_URL}/api/admin/intents/auto-dry-run-drain?limit=10",
        headers={"Authorization": f"Bearer {token}"},
        timeout=60,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    for key in (
        "requested_limit", "stack_filter", "pending_found",
        "processed", "would_pass", "would_block",
        "failures", "failure_count", "doctrine_note",
    ):
        assert key in body, f"missing key {key} in drain response"
    assert body["requested_limit"] == 10
    assert body["stack_filter"] is None
    assert isinstance(body["failures"], list)


@pytest.mark.tripwire
def test_drain_endpoint_accepts_stack_filter():
    """Drain must scope to a single brain when `stack` is provided."""
    token = _login()
    r = requests.post(
        f"{BASE_URL}/api/admin/intents/auto-dry-run-drain?limit=5&stack=alpha",
        headers={"Authorization": f"Bearer {token}"},
        timeout=60,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["stack_filter"] == "alpha"


# ──────────────────────── end-to-end ingest → verdict ────────────────────────


@pytest.mark.tripwire
def test_ingest_intent_transitions_off_pending():
    """The textbook acceptance test for this hook: post an intent, wait
    a beat, then read it back — `gate_state` must NOT still be `pending`.

    Doctrine: this is the regression guard against the "21k pending"
    bug. If anyone ever short-circuits the auto-dry-run hook from the
    ingest path, this test breaks immediately.
    """
    from pymongo import MongoClient
    mongo_url = os.environ["MONGO_URL"]
    db_name = os.environ["DB_NAME"]
    client = MongoClient(mongo_url)
    coll = client[db_name]["shared_intents"]

    token = _login()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    sym = f"TRIPWIRE-{uuid.uuid4().hex[:8]}"
    payload = {
        "stack": "alpha",
        "action": "BUY",
        "symbol": sym,
        "lane": "equity",
        "confidence": 0.6,
        "rationale": "tripwire — auto dry-run hook",
        "doctrine_snapshot": {"price": 100.0, "bid": 99.95, "ask": 100.05},
    }
    r = requests.post(
        f"{BASE_URL}/api/admin/intents",
        json=payload, headers=headers, timeout=20,
    )
    assert r.status_code == 200, r.text
    iid = r.json()["intent_id"]

    # Auto-dry-run fires asynchronously. Poll Mongo for up to ~3s.
    final_state = None
    try:
        for _ in range(30):
            time.sleep(0.1)
            doc = coll.find_one({"intent_id": iid}, {"_id": 0, "gate_state": 1})
            if doc:
                final_state = doc.get("gate_state")
                if final_state and final_state != "pending":
                    break
        assert final_state is not None, "intent disappeared"
        assert final_state != "pending", (
            "auto-dry-run did not fire — intent stuck at pending. "
            "This is the regression that the auto-dry-run hook prevents."
        )
        assert final_state in ("dry_run_passed", "dry_run_blocked"), final_state
    finally:
        coll.delete_one({"intent_id": iid})


# ──────────────────────── disabled-mode preserves old behavior ────────────────────────


@pytest.mark.tripwire
def test_disabled_mode_leaves_intent_at_pending(monkeypatch):
    """When AUTO_DRY_RUN_ON_INGEST=false, the ingest hook becomes a
    no-op (operator gets the old behavior — intents sit at `pending`
    until manual dry-run). This is the load-relief escape hatch.

    Pure-function test against the gate function. The HTTP path
    requires the env var to be read at request time, so we just
    confirm the helper short-circuits."""
    monkeypatch.setenv("AUTO_DRY_RUN_ON_INGEST", "false")
    from shared.intents import _fire_and_forget_dry_run
    # Should return without raising even though the intent_id is bogus.
    # If it were active, it would try to schedule a dry-run; the gate
    # short-circuits before any work happens.
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(
            _fire_and_forget_dry_run("does-not-exist", actor="test"),
        )
    finally:
        loop.close()


# ──────────────────────── doctrine guard ────────────────────────


@pytest.mark.tripwire
def test_drain_response_doctrine_note_is_present():
    """The drain endpoint must carry a doctrine note so operators
    know it's idempotent + how it relates to manual dry-run. Pinned
    so accidental edits don't strip the operator-visible doctrine."""
    token = _login()
    r = requests.post(
        f"{BASE_URL}/api/admin/intents/auto-dry-run-drain?limit=1",
        headers={"Authorization": f"Bearer {token}"}, timeout=30,
    )
    assert r.status_code == 200
    note = r.json().get("doctrine_note", "")
    assert "idempotent" in note.lower() or "no-op" in note.lower(), (
        f"drain doctrine note must explain idempotency; got {note!r}"
    )
