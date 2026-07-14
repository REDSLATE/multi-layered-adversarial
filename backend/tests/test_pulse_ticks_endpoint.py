"""Test /api/mc/pulse-health/ticks — the operator dashboard readout
that closes the observability gap that let the P0 pulse silence go
undetected for weeks.
"""
from __future__ import annotations

import os

import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")
if not BASE_URL:
    with open("/app/frontend/.env") as _f:
        for _ln in _f:
            if _ln.startswith("REACT_APP_BACKEND_URL="):
                BASE_URL = _ln.split("=", 1)[1].strip().strip('"').rstrip("/")
                break

ADMIN_EMAIL = "admin@risedual.io"
ADMIN_PASSWORD = "risedual-admin-2026"


@pytest.fixture(scope="module")
def auth_headers() -> dict:
    r = requests.post(
        f"{BASE_URL}/api/auth/login",
        json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
        timeout=10,
    )
    assert r.status_code == 200, r.text
    tok = r.json().get("access_token") or r.json().get("token")
    return {"Authorization": f"Bearer {tok}"}


def test_ticks_returns_200_with_shape(auth_headers):
    r = requests.get(
        f"{BASE_URL}/api/mc/pulse-health/ticks?limit=5",
        headers=auth_headers, timeout=10,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert "count" in body and "ticks" in body
    assert isinstance(body["ticks"], list)
    if body["ticks"]:
        t = body["ticks"][0]
        # Every field the OperatorControl UI reads must be present.
        for k in (
            "pulse_id", "started_at", "runtime_mode", "snapshot_count",
            "brains_completed", "brains_failed", "arbitrations_completed",
            "intents_emitted", "orchestration_ok", "overrun",
            "brains_completed_count", "brains_failed_count",
        ):
            assert k in t, f"missing field on tick: {k!r}"


def test_ticks_route_not_shadowed_by_brain_id(auth_headers):
    """Route ordering guard: `/ticks` MUST be registered before
    `/{brain_id}` or FastAPI treats "ticks" as a brain identifier
    and returns a pulse_health payload instead. This test caught
    exactly that regression during initial development."""
    r = requests.get(
        f"{BASE_URL}/api/mc/pulse-health/ticks",
        headers=auth_headers, timeout=10,
    )
    assert r.status_code == 200
    body = r.json()
    # The `/{brain_id}` endpoint returns a `brain` key at top level.
    # The `/ticks` endpoint returns `ok` + `ticks`. If we see `brain`
    # here, the route is shadowed.
    assert "ticks" in body, (
        f"/ticks appears shadowed by /{{brain_id}} — got body keys: "
        f"{list(body.keys())}"
    )
    assert "brain" not in body or body.get("ticks") is not None


def test_ticks_limit_bounds(auth_headers):
    # Below min
    r = requests.get(
        f"{BASE_URL}/api/mc/pulse-health/ticks?limit=0",
        headers=auth_headers, timeout=10,
    )
    assert r.status_code in {400, 422}
    # Above max
    r = requests.get(
        f"{BASE_URL}/api/mc/pulse-health/ticks?limit=999",
        headers=auth_headers, timeout=10,
    )
    assert r.status_code in {400, 422}
    # Valid
    r = requests.get(
        f"{BASE_URL}/api/mc/pulse-health/ticks?limit=1",
        headers=auth_headers, timeout=10,
    )
    assert r.status_code == 200
    assert len(r.json().get("ticks", [])) <= 1


def test_ticks_requires_auth():
    r = requests.get(
        f"{BASE_URL}/api/mc/pulse-health/ticks",
        timeout=10,
    )
    assert r.status_code in {401, 403}
