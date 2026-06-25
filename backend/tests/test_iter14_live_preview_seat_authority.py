"""Iter14 — live preview integration check for seat-authority doctrine.

We can NOT exercise the full 403-on-non-seat-holder path against the
live preview (the seat holder there is whatever the operator pinned),
but we CAN pin the cheap, non-stateful invariants:

  A. POST /api/execution/submit with brain_name OMITTED returns 422
     (Pydantic required-field validation triggers BEFORE business logic
     so this is safe to fire against prod-shaped data).
  B. POST /api/execution/submit-override with override_reason < 12
     chars returns 400 with the "at least 12 characters" message.
  C. POST /api/execution/submit on a synthetic intent_id that doesn't
     exist must NOT pass the brain_name validation silently — when
     brain_name is provided, the 404/lookup-failure error code surfaces
     instead of 422.

These run with admin@risedual.io credentials.
"""
from __future__ import annotations

import os
import pytest
import requests

BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")  # from frontend env

ADMIN_EMAIL = "admin@risedual.io"
ADMIN_PASS = "risedual-admin-2026"


@pytest.fixture(scope="module")
def admin_token():
    r = requests.post(
        f"{BASE_URL}/api/auth/login",
        json={"email": ADMIN_EMAIL, "password": ADMIN_PASS},
        timeout=15,
    )
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text[:200]}"
    tok = r.json().get("access_token")
    assert tok, "no access_token in login response"
    return tok


@pytest.fixture
def auth_headers(admin_token):
    return {
        "Authorization": f"Bearer {admin_token}",
        "Content-Type": "application/json",
    }


# A. Missing brain_name → 422 (Pydantic required field)
def test_submit_missing_brain_name_returns_422(auth_headers):
    payload = {
        "intent_id": "sa-test-live-missing-brain",
        "order_notional_usd": 10.0,
        "confirm": "execute",
        # brain_name OMITTED
    }
    r = requests.post(
        f"{BASE_URL}/api/execution/submit",
        json=payload,
        headers=auth_headers,
        timeout=15,
    )
    assert r.status_code == 422, (
        f"expected 422 (brain_name required) got {r.status_code} {r.text[:300]}"
    )
    body = r.json()
    txt = str(body).lower()
    assert "brain_name" in txt, f"422 should mention brain_name: {body}"


# B. /execution/submit-override with short reason → 400
def test_submit_override_short_reason_returns_400(auth_headers):
    payload = {
        "intent_id": "sa-test-live-short-reason",
        "order_notional_usd": 10.0,
        "confirm": "execute",
        "operator_override": True,
        "override_reason": "too short",  # < 12 chars
        "brain_name": "camino",
    }
    r = requests.post(
        f"{BASE_URL}/api/execution/submit-override",
        json=payload,
        headers=auth_headers,
        timeout=15,
    )
    assert r.status_code == 400, (
        f"expected 400 short-reason got {r.status_code} {r.text[:300]}"
    )
    assert "at least 12 characters" in r.text, (
        f"missing the 12-char gate message: {r.text[:300]}"
    )


# C. brain_name provided but intent doesn't exist → must NOT be 422
def test_submit_with_brain_name_passes_pydantic_layer(auth_headers):
    """If we supply brain_name, we should clear the Pydantic layer.
    Whatever error comes back must NOT be a 422 about brain_name."""
    payload = {
        "intent_id": "sa-test-live-nonexistent-intent-zzz",
        "order_notional_usd": 10.0,
        "confirm": "execute",
        "brain_name": "camino",
    }
    r = requests.post(
        f"{BASE_URL}/api/execution/submit",
        json=payload,
        headers=auth_headers,
        timeout=15,
    )
    # Anything but 422-due-to-brain_name is acceptable here. The
    # endpoint will reject because the intent doesn't exist (404/400/
    # 403 etc.) — we just want to prove the validator gate moved on.
    if r.status_code == 422:
        body_txt = r.text.lower()
        assert "brain_name" not in body_txt, (
            f"422 still complaining about brain_name even when provided: {body_txt[:300]}"
        )


# D. /execution/submit-override also requires brain_name → 422
def test_submit_override_missing_brain_name_returns_422(auth_headers):
    payload = {
        "intent_id": "sa-test-live-override-missing-brain",
        "order_notional_usd": 10.0,
        "confirm": "execute",
        "operator_override": True,
        "override_reason": "explicit operator authorization for non-holder",
        # brain_name OMITTED
    }
    r = requests.post(
        f"{BASE_URL}/api/execution/submit-override",
        json=payload,
        headers=auth_headers,
        timeout=15,
    )
    assert r.status_code == 422, (
        f"expected 422 override missing brain_name got {r.status_code} {r.text[:300]}"
    )
    assert "brain_name" in r.text.lower()
