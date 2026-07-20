"""Iteration 28 tests — validate:

  * Auto-router status surface (new fields) + force-tick returns
    without TimeoutError (results may be 0 because arbiter is
    DISARMED in preview — that is expected, not a bug).
  * Kill-map returns 200 with all stages + coherent verdict at
    both 24h and 168h windows; stage_3_ingest contains
    intents_created (no crash).
  * /api/admin/nuke-test-data and /api/admin/emergency-purge*
    routes are gone (404).
"""
from __future__ import annotations

import os
import time
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")
# The frontend/.env is the source of truth per env spec, but pytest is
# run inside backend context; fall back to a local read to be safe.
if not BASE_URL:
    try:
        with open("/app/frontend/.env") as fh:
            for line in fh:
                if line.startswith("REACT_APP_BACKEND_URL="):
                    BASE_URL = line.strip().split("=", 1)[1].strip('"').rstrip("/")
                    break
    except Exception:
        pass
assert BASE_URL, "REACT_APP_BACKEND_URL not resolvable"


@pytest.fixture(scope="module")
def token() -> str:
    r = requests.post(
        f"{BASE_URL}/api/auth/login",
        json={"email": "admin@risedual.io", "password": "risedual-admin-2026"},
        timeout=15,
    )
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text[:200]}"
    data = r.json()
    tok = data.get("access_token") or data.get("token")
    assert tok, f"no token in response: {data}"
    return tok


@pytest.fixture(scope="module")
def auth_headers(token) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ────────────────────────────────────────────────────────────────
# 1. Login smoke
# ────────────────────────────────────────────────────────────────
def test_admin_login_returns_token(token):
    assert isinstance(token, str) and len(token) > 10


# ────────────────────────────────────────────────────────────────
# 2. Auto-router status — new fields present, task alive, no error
# ────────────────────────────────────────────────────────────────
def test_auto_router_status_shape(auth_headers):
    r = requests.get(
        f"{BASE_URL}/api/admin/auto-router/status",
        headers=auth_headers, timeout=20,
    )
    assert r.status_code == 200, f"status {r.status_code}: {r.text[:200]}"
    data = r.json()
    # Legacy fields still there
    for key in ("task_alive", "task_done", "tick_count", "last_tick_ts",
                "last_tick_error", "interval_sec"):
        assert key in data, f"missing legacy field {key} in {list(data.keys())}"
    # NEW fields from iteration 28
    assert "last_tick_route_timeouts" in data, "missing last_tick_route_timeouts"
    assert "last_tick_deferred" in data, "missing last_tick_deferred"
    assert "route_budget_sec" in data, "missing route_budget_sec"
    assert data["route_budget_sec"] == 35.0, (
        f"route_budget_sec expected 35.0 got {data['route_budget_sec']}"
    )
    # Task should be alive and last tick error should be None (no
    # TimeoutError from webull hangs).
    assert data["task_alive"] is True, f"task not alive: {data}"
    assert data["last_tick_error"] is None, (
        f"last_tick_error non-null: {data['last_tick_error']}"
    )


# ────────────────────────────────────────────────────────────────
# 3. Force-tick returns ok=true with no TimeoutError
# ────────────────────────────────────────────────────────────────
def test_auto_router_force_tick(auth_headers):
    r = requests.post(
        f"{BASE_URL}/api/admin/auto-router/force-tick",
        headers=auth_headers, timeout=120,
    )
    assert r.status_code == 200, f"status {r.status_code}: {r.text[:200]}"
    data = r.json()
    assert data.get("ok") is True, f"force-tick returned not-ok: {data}"
    # results_count can be 0 in preview (DISARMED arbiter, no pending
    # intents) — that's expected. Just make sure there was no error.
    assert data.get("error") in (None, ""), f"force-tick error: {data}"
    assert "results_count" in data


# ────────────────────────────────────────────────────────────────
# 4. Kill-map 24h — all stages, verdict, stage_3.intents_created
# ────────────────────────────────────────────────────────────────
def test_kill_map_24h(auth_headers):
    r = requests.get(
        f"{BASE_URL}/api/admin/kill-map?hours=24",
        headers=auth_headers, timeout=45,
    )
    assert r.status_code == 200, f"status {r.status_code}: {r.text[:200]}"
    data = r.json()
    for stage in ("stage_1_pulse", "stage_2_arbiter", "stage_3_ingest",
                  "stage_4_top_block_reasons", "stage_5_broker", "verdict"):
        assert stage in data, f"missing {stage} in kill-map response keys={list(data.keys())}"
    verdict = data["verdict"]
    assert isinstance(verdict, str) and len(verdict) > 0, f"bad verdict {verdict!r}"
    # stage_3_ingest MUST contain intents_created (no crash, per fix)
    s3 = data["stage_3_ingest"]
    assert isinstance(s3, dict), f"stage_3_ingest not dict: {type(s3)}"
    assert "intents_created" in s3, (
        f"stage_3_ingest missing intents_created: {list(s3.keys())}"
    )
    assert isinstance(s3["intents_created"], int), (
        f"intents_created not int: {type(s3['intents_created'])}"
    )
    # If stage-3 aggregation errored, verdict should say READ ERROR
    if s3.get("error"):
        assert "STAGE 3 READ ERROR" in verdict or "STAGE 2" in verdict or "STAGE 1" in verdict, (
            f"stage-3 error but verdict doesn't reflect it honestly: {verdict}"
        )


# ────────────────────────────────────────────────────────────────
# 5. Kill-map 168h — 7-day window still returns 200 in reasonable time
# ────────────────────────────────────────────────────────────────
def test_kill_map_168h(auth_headers):
    t0 = time.time()
    r = requests.get(
        f"{BASE_URL}/api/admin/kill-map?hours=168",
        headers=auth_headers, timeout=60,
    )
    elapsed = time.time() - t0
    assert r.status_code == 200, f"status {r.status_code}: {r.text[:200]}"
    assert elapsed < 55, f"kill-map 168h took {elapsed:.1f}s (too slow)"
    data = r.json()
    assert data.get("window_hours") == 168
    assert "stage_3_ingest" in data
    assert "intents_created" in data["stage_3_ingest"]


# ────────────────────────────────────────────────────────────────
# 6. nuke-test-data route is REMOVED (404)
# ────────────────────────────────────────────────────────────────
def test_nuke_test_data_gone(auth_headers):
    # Both POST and GET should 404 — route deleted from router_registry.
    r = requests.post(
        f"{BASE_URL}/api/admin/nuke-test-data",
        headers=auth_headers, timeout=15,
    )
    assert r.status_code == 404, (
        f"nuke-test-data still live: {r.status_code} {r.text[:200]}"
    )


# ────────────────────────────────────────────────────────────────
# 7. emergency-purge route is REMOVED (404)
# ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("method", ["get", "post"])
def test_emergency_purge_gone(auth_headers, method):
    fn = getattr(requests, method)
    r = fn(
        f"{BASE_URL}/api/admin/emergency-purge",
        headers=auth_headers, timeout=15,
    )
    assert r.status_code == 404, (
        f"emergency-purge still live via {method.upper()}: "
        f"{r.status_code} {r.text[:200]}"
    )
