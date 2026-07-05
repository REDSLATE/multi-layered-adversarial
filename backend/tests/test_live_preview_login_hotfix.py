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
# The /api/admin/brain-metrics endpoints were retired 2026-02-28.
# They were half-broken (referenced deleted PIPELINE_RECEIPTS_COLL)
# and had no frontend consumer. Live-path metrics now live at
# /api/admin/intent-clearance-funnel.


# ── Roster ────────────────────────────────────────────────────────
def test_admin_roster_seats(admin_token):
    """Live-preview smoke test for /api/admin/roster.

    Doctrine (2026-02-28): assert SCHEMA only — the 8 canonical role
    keys must be present and typed correctly. Do NOT assert specific
    seat holders — those rotate via the operator UI, and pinning them
    here creates false-positive test failures every time a seat is
    reassigned. The seat-authority regression is fenced by
    `test_seat_reads_canonical_roster.py` in unit form."""
    r = requests.get(
        f"{BASE_URL}/api/admin/roster",
        headers={"Authorization": f"Bearer {admin_token}"},
        timeout=30,
    )
    assert r.status_code == 200, f"got {r.status_code}: {r.text[:400]}"
    data = r.json()
    expected = {
        "strategist", "executor", "governor", "auditor",
        "crypto_strategist", "crypto", "crypto_governor", "crypto_auditor",
    }
    seats = data.get("assignments") or data.get("seats") or data
    assert isinstance(seats, dict), f"unexpected roster shape: {data}"
    missing = expected - set(seats.keys())
    assert not missing, f"missing seat keys: {missing}; got {list(seats.keys())}"
    # Every value must be either a brain name (str) or None (vacant).
    for role, holder in seats.items():
        if role not in expected:
            continue
        assert holder is None or isinstance(holder, str), (
            f"seat {role} holder has unexpected type: {type(holder)}"
        )

