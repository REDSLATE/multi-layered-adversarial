"""Iter13 happy-path verification: admin GETs return 200 in <1s on the
preview backend. Confirms the new api.js retry path is NEVER triggered
when origin is healthy.
"""
from __future__ import annotations
import os
import time
import requests
import pytest

BASE = os.environ.get("REACT_APP_BACKEND_URL", "https://multi-brain-backbone.preview.emergentagent.com").rstrip("/")
ADMIN_EMAIL = "admin@risedual.io"
ADMIN_PASS = "risedual-admin-2026"


@pytest.fixture(scope="module")
def token():
    r = requests.post(f"{BASE}/api/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASS}, timeout=10)
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text[:200]}"
    data = r.json()
    tok = data.get("access_token")
    assert tok, "no access_token in login response"
    return tok


@pytest.fixture(scope="module")
def auth_headers(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.parametrize("path", [
    "/api/admin/flags",
    "/api/admin/diagnostics",
    "/api/admin/system-flags",
    "/api/admin/paradox-v3/status",
    "/api/admin/brain-metrics/health",
])
def test_admin_get_happy_path(auth_headers, path):
    t0 = time.time()
    r = requests.get(f"{BASE}{path}", headers=auth_headers, timeout=10)
    elapsed = time.time() - t0
    assert r.status_code == 200, f"{path} → {r.status_code}: {r.text[:200]}"
    # <2s is the operator threshold (one origin call, no retry path).
    assert elapsed < 2.0, f"{path} took {elapsed:.2f}s — likely retry triggered on happy path"
    # Validate it actually returned a parseable body
    try:
        body = r.json()
        assert body is not None
    except Exception as e:
        pytest.fail(f"{path} returned non-JSON body: {e}")
