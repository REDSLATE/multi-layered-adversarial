"""Live HTTP test for /api/admin/execution-lifecycle/funnel.

Iteration 16 P3 — Execution Lifecycle Funnel tile API verification.
Hits the deployed preview backend at REACT_APP_BACKEND_URL using the
seeded admin credentials.
"""
from __future__ import annotations

import os
import pytest
import requests

BASE_URL = os.environ.get(
    "REACT_APP_BACKEND_URL",
    "https://multi-brain-backbone.preview.emergentagent.com",
).rstrip("/")
LOGIN = {"email": "admin@risedual.io", "password": "risedual-admin-2026"}

EXPECTED_BUCKETS = {"filled", "partially_filled", "working", "canceled", "unknown"}


@pytest.fixture(scope="module")
def admin_token():
    r = requests.post(f"{BASE_URL}/api/auth/login", json=LOGIN, timeout=20)
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text[:200]}"
    tok = r.json().get("access_token")
    assert tok, "no access_token in login response"
    return tok


@pytest.fixture(scope="module")
def auth_headers(admin_token):
    return {"Authorization": f"Bearer {admin_token}"}


def _get_funnel(headers, **params):
    return requests.get(
        f"{BASE_URL}/api/admin/execution-lifecycle/funnel",
        headers=headers, params=params, timeout=20,
    )


# ---- Response shape (default 24h, no lane) ----
def test_funnel_default_shape(auth_headers):
    r = _get_funnel(auth_headers)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["ok"] is True
    assert d["window_hours"] == 24
    assert d["lane_filter"] is None
    assert isinstance(d["since"], str) and "T" in d["since"]
    assert isinstance(d["total_executed"], int) and d["total_executed"] >= 0
    assert set(d["bucket_counts"].keys()) == EXPECTED_BUCKETS
    assert set(d["bucket_percentages"].keys()) == EXPECTED_BUCKETS
    assert d["bucket_order"] == ["filled", "partially_filled", "working", "canceled", "unknown"]
    assert set(d["by_lane"].keys()) == {"equity", "crypto"}
    for lane_buckets in d["by_lane"].values():
        assert set(lane_buckets.keys()) == EXPECTED_BUCKETS
    assert isinstance(d["by_brain"], dict)
    assert isinstance(d["unknown_samples"], list)
    assert len(d["unknown_samples"]) <= 10
    assert isinstance(d["doctrine_note"], str) and len(d["doctrine_note"]) > 20

    # counts sum == total
    assert sum(d["bucket_counts"].values()) == d["total_executed"]
    # percentages sum ~100 (or 0 when empty)
    pct_sum = sum(d["bucket_percentages"].values())
    if d["total_executed"] == 0:
        assert pct_sum == 0.0
    else:
        assert 99.5 <= pct_sum <= 100.5, f"pct sum={pct_sum}"


# ---- Historical window (720h max) ----
def test_funnel_720h_window(auth_headers):
    r = _get_funnel(auth_headers, hours=720)
    assert r.status_code == 200
    d = r.json()
    assert d["window_hours"] == 720
    assert sum(d["bucket_counts"].values()) == d["total_executed"]


# ---- Lane filter: equity ----
def test_funnel_lane_equity_excludes_crypto(auth_headers):
    r = _get_funnel(auth_headers, lane="equity", hours=720)
    assert r.status_code == 200
    d = r.json()
    assert d["lane_filter"] == "equity"
    # crypto sub-bucket should be all zeros
    assert all(v == 0 for v in d["by_lane"]["crypto"].values()), d["by_lane"]


# ---- Lane filter: crypto ----
def test_funnel_lane_crypto_excludes_equity(auth_headers):
    r = _get_funnel(auth_headers, lane="crypto", hours=720)
    assert r.status_code == 200
    d = r.json()
    assert d["lane_filter"] == "crypto"
    assert all(v == 0 for v in d["by_lane"]["equity"].values()), d["by_lane"]


# ---- Unknown lane string ignored (treated as no filter) ----
def test_funnel_invalid_lane_ignored(auth_headers):
    r = _get_funnel(auth_headers, lane="foo")
    assert r.status_code == 200
    d = r.json()
    # endpoint echoes lane_filter as given but ignores it for the query
    assert d["lane_filter"] == "foo"
    # since lane='foo' doesn't filter, both lanes should be available
    # (sum equals total_executed across both)
    assert sum(d["bucket_counts"].values()) == d["total_executed"]


# ---- Bounds validation ----
def test_funnel_hours_zero_rejected(auth_headers):
    r = _get_funnel(auth_headers, hours=0)
    assert r.status_code == 422


def test_funnel_hours_over_max_rejected(auth_headers):
    r = _get_funnel(auth_headers, hours=721)
    assert r.status_code == 422


# ---- Auth required ----
def test_funnel_requires_auth():
    r = requests.get(
        f"{BASE_URL}/api/admin/execution-lifecycle/funnel", timeout=20,
    )
    assert r.status_code in (401, 403), r.status_code
