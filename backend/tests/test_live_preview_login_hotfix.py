"""Live preview verification for login hotfix + brain-metrics + roster.

Hits the public preview URL (REACT_APP_BACKEND_URL) to ensure the
deployed backend reflects the hotfix and the prior-session endpoints
still work.
"""
from __future__ import annotations

import os
import pytest
import requests


BASE_URL = os.environ.get(
    "REACT_APP_BACKEND_URL",
    "https://multi-brain-backbone.preview.emergentagent.com",
).rstrip("/")

ADMIN_EMAIL = "admin@risedual.io"
ADMIN_PASSWORD = "risedual-admin-2026"


@pytest.fixture(scope="module")
def admin_token() -> str:
    r = requests.post(
        f"{BASE_URL}/api/auth/login",
        json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
        timeout=30,
    )
    assert r.status_code == 200, f"admin login failed: {r.status_code} {r.text}"
    data = r.json()
    assert "access_token" in data
    return data["access_token"]


# ── Auth ──────────────────────────────────────────────────────────
def test_login_success_returns_access_token():
    r = requests.post(
        f"{BASE_URL}/api/auth/login",
        json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
        timeout=30,
    )
    assert r.status_code == 200, f"got {r.status_code}: {r.text}"
    data = r.json()
    assert isinstance(data.get("access_token"), str) and len(data["access_token"]) > 20
    assert data.get("token_type") == "bearer"
    assert data["user"]["email"] == ADMIN_EMAIL
    assert data["user"]["role"] == "admin"


def test_login_wrong_password_returns_401():
    r = requests.post(
        f"{BASE_URL}/api/auth/login",
        json={"email": ADMIN_EMAIL, "password": "definitely-wrong-PASSWORD-xxx"},
        timeout=30,
    )
    assert r.status_code == 401, f"got {r.status_code}: {r.text}"


def test_auth_me_with_token(admin_token):
    r = requests.get(
        f"{BASE_URL}/api/auth/me",
        headers={"Authorization": f"Bearer {admin_token}"},
        timeout=30,
    )
    assert r.status_code == 200
    me = r.json()
    assert me["email"] == ADMIN_EMAIL


# ── Brain metrics ─────────────────────────────────────────────────
def test_brain_metrics_24h(admin_token):
    r = requests.get(
        f"{BASE_URL}/api/admin/brain-metrics?hours=24",
        headers={"Authorization": f"Bearer {admin_token}"},
        timeout=60,
    )
    assert r.status_code == 200, f"got {r.status_code}: {r.text[:400]}"
    data = r.json()
    # KPI payload — accept either flat or nested. We just verify it's
    # a dict with content.
    assert isinstance(data, dict)
    assert len(data) > 0


def test_brain_metrics_history_72h(admin_token):
    r = requests.get(
        f"{BASE_URL}/api/admin/brain-metrics/history?hours=72",
        headers={"Authorization": f"Bearer {admin_token}"},
        timeout=60,
    )
    assert r.status_code == 200, f"got {r.status_code}: {r.text[:400]}"
    data = r.json()
    assert isinstance(data, (list, dict))


# ── Roster ────────────────────────────────────────────────────────
def test_admin_roster_seats(admin_token):
    r = requests.get(
        f"{BASE_URL}/api/admin/roster",
        headers={"Authorization": f"Bearer {admin_token}"},
        timeout=30,
    )
    assert r.status_code == 200, f"got {r.status_code}: {r.text[:400]}"
    data = r.json()
    # Expected 8 seat keys
    expected = {
        "strategist", "executor", "governor", "auditor",
        "crypto_strategist", "crypto", "crypto_governor", "crypto_auditor",
    }
    # Seats live under `assignments` in this API shape.
    seats = data.get("assignments") or data.get("seats") or data
    assert isinstance(seats, dict), f"unexpected roster shape: {data}"
    missing = expected - set(seats.keys())
    assert not missing, f"missing seat keys: {missing}; got {list(seats.keys())}"
    # Spot check known values from the operator's request.
    assert seats["executor"] == "camino", f"executor={seats['executor']}"
    assert seats["strategist"] == "barracuda", f"strategist={seats['strategist']}"
    assert seats["governor"] == "hellcat"
    assert seats["auditor"] == "gto"
    print(f"seat map: {seats}")
