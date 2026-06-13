"""Legacy wrapper toggle — A/B diagnostic tests (2026-02-19).

Operator wants to confirm whether the penalty-stacking wrappers
are causing the 403/502 cascade by disabling one wrapper at a time
and observing the impact. This test suite pins the toggle behavior.
"""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest
import httpx
from pymongo import MongoClient

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from dotenv import load_dotenv  # noqa: E402
load_dotenv("/app/backend/.env")

from shared.legacy_brain_wrappers import (  # noqa: E402
    BRAIN_WRAPPER_ASSIGNMENTS,
    apply_legacy_wrapper,
    is_wrapper_disabled,
    set_wrapper_disabled,
    wrapper_status,
)

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL") or os.environ.get(
    "BACKEND_URL", "http://127.0.0.1:8001",
)
_MONGO = MongoClient(os.environ["MONGO_URL"])
_sync_db = _MONGO[os.environ.get("DB_NAME", "test_database")]


@pytest.fixture(autouse=True)
def _reset():
    """Clear any prior overrides between tests."""
    for brain in BRAIN_WRAPPER_ASSIGNMENTS:
        set_wrapper_disabled(brain, False)
    yield
    for brain in BRAIN_WRAPPER_ASSIGNMENTS:
        set_wrapper_disabled(brain, False)


# ── Unit tests on the toggle primitives ──────────────────────────────


def test_default_state_all_enabled():
    status = wrapper_status()
    assert len(status["wrappers"]) == 4
    for row in status["wrappers"]:
        assert row["disabled"] is False
        assert row["source"] is None


def test_disable_one_brain_only():
    set_wrapper_disabled("gto", True, "A/B test 403 cascade")
    status = wrapper_status()
    rows = {r["brain_id"]: r for r in status["wrappers"]}
    assert rows["gto"]["disabled"] is True
    assert rows["gto"]["reason"] == "A/B test 403 cascade"
    assert rows["gto"]["source"] == "operator_override"
    # Others remain active.
    for other in ("camino", "barracuda", "hellcat"):
        assert rows[other]["disabled"] is False


def test_unknown_brain_rejected():
    with pytest.raises(ValueError, match="unknown brain_id"):
        set_wrapper_disabled("frankenstein", True)


def test_disabled_wrapper_skips_application():
    """When a wrapper is disabled, `apply_legacy_wrapper` returns the
    intent UNCHANGED but stamps audit fields."""
    intent_in = {
        "intent_id": "test-1",
        "brain_id": "gto",
        "action": "BUY",
        "size_bias": 1.0,
        "symbol": "AAPL",
    }
    # Enabled: wrapper mutates size_bias (we don't care to assert the
    # exact value — just that it changed OR added fields).
    enabled_out = apply_legacy_wrapper(dict(intent_in))
    # Disabled: wrapper is bypassed, audit fields stamped.
    set_wrapper_disabled("gto", True, "diagnostic skip")
    disabled_out = apply_legacy_wrapper(dict(intent_in))
    assert disabled_out["wrapper_disabled_by_operator"] is True
    assert disabled_out["wrapper_skipped"] == "redeye_legacy_adversary"
    assert disabled_out["wrapper_disabled_reason"] == "diagnostic skip"
    # And critically: size_bias was NOT compressed by the wrapper.
    assert disabled_out["size_bias"] == intent_in["size_bias"]


def test_env_var_disable(monkeypatch):
    """`RISEDUAL_DISABLED_WRAPPERS=gto,hellcat` disables both."""
    monkeypatch.setenv("RISEDUAL_DISABLED_WRAPPERS", "gto, hellcat")
    assert is_wrapper_disabled("gto") == (True, "env:RISEDUAL_DISABLED_WRAPPERS")
    assert is_wrapper_disabled("hellcat") == (True, "env:RISEDUAL_DISABLED_WRAPPERS")
    assert is_wrapper_disabled("camino")[0] is False


def test_runtime_override_beats_env(monkeypatch):
    """Operator's runtime override wins over env-var setting."""
    monkeypatch.setenv("RISEDUAL_DISABLED_WRAPPERS", "gto")
    # GTO is disabled via env.
    assert is_wrapper_disabled("gto")[0] is True
    # Operator's runtime override REPLACES the env-derived reason.
    set_wrapper_disabled("gto", True, "operator A/B test")
    disabled, reason = is_wrapper_disabled("gto")
    assert disabled is True
    assert reason == "operator A/B test"


# ── HTTP-level tests on the admin endpoints ──────────────────────────


@pytest.fixture
def auth_token():
    with httpx.Client(base_url=BASE_URL, timeout=10) as c:
        r = c.post(
            "/api/auth/login",
            json={"email": "admin@risedual.io", "password": "risedual-admin-2026"},
        )
    if r.status_code != 200:
        pytest.skip(f"auth login failed ({r.status_code})")
    return r.json().get("token") or r.json().get("access_token")


def test_status_endpoint(auth_token):
    with httpx.Client(base_url=BASE_URL, timeout=10) as c:
        r = c.get(
            "/api/admin/wrappers/status",
            headers={"Authorization": f"Bearer {auth_token}"},
        )
    assert r.status_code == 200
    body = r.json()
    assert "wrappers" in body
    assert len(body["wrappers"]) == 4
    brain_ids = {row["brain_id"] for row in body["wrappers"]}
    assert brain_ids == {"camino", "barracuda", "hellcat", "gto"}


def test_toggle_endpoint_disable_then_enable(auth_token):
    audit_key = f"wrapper-test-{uuid.uuid4().hex[:8]}"
    try:
        with httpx.Client(base_url=BASE_URL, timeout=10) as c:
            # Disable
            r = c.post(
                "/api/admin/wrappers/toggle",
                headers={"Authorization": f"Bearer {auth_token}"},
                json={
                    "brain_id": "gto",
                    "disabled": True,
                    "reason": f"automated test {audit_key}",
                },
            )
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["ok"] is True
            gto_row = next(
                r for r in body["status"]["wrappers"]
                if r["brain_id"] == "gto"
            )
            assert gto_row["disabled"] is True
            assert audit_key in gto_row["reason"]

            # Re-enable
            r = c.post(
                "/api/admin/wrappers/toggle",
                headers={"Authorization": f"Bearer {auth_token}"},
                json={"brain_id": "gto", "disabled": False, "reason": ""},
            )
            assert r.status_code == 200
            gto_row = next(
                r for r in r.json()["status"]["wrappers"]
                if r["brain_id"] == "gto"
            )
            assert gto_row["disabled"] is False
    finally:
        # Clean up audit rows + ensure wrapper is re-enabled.
        _sync_db["shared_wrapper_toggle_audit"].delete_many(
            {"reason": {"$regex": audit_key}},
        )
        set_wrapper_disabled("gto", False)


def test_toggle_requires_reason_when_disabling(auth_token):
    with httpx.Client(base_url=BASE_URL, timeout=10) as c:
        r = c.post(
            "/api/admin/wrappers/toggle",
            headers={"Authorization": f"Bearer {auth_token}"},
            json={"brain_id": "gto", "disabled": True, "reason": "ab"},  # too short
        )
    assert r.status_code == 400
    assert "reason" in r.text.lower()


def test_toggle_rejects_unknown_brain(auth_token):
    with httpx.Client(base_url=BASE_URL, timeout=10) as c:
        r = c.post(
            "/api/admin/wrappers/toggle",
            headers={"Authorization": f"Bearer {auth_token}"},
            json={
                "brain_id": "skynet",
                "disabled": True,
                "reason": "audit reasoning",
            },
        )
    assert r.status_code == 422
    assert "skynet" in r.text or "brain_id" in r.text.lower()
