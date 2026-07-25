"""Live API regression tests for Gain Goal admin endpoints.
Uses REACT_APP_BACKEND_URL. Restores crypto config to nulls at the end.
"""
import os
import pytest
import requests

BASE_URL = "https://multi-brain-backbone.preview.emergentagent.com"
ADMIN_EMAIL = "admin@risedual.io"
ADMIN_PASS = "risedual-admin-2026"


@pytest.fixture(scope="module")
def token():
    r = requests.post(f"{BASE_URL}/api/auth/login",
                      json={"email": ADMIN_EMAIL, "password": ADMIN_PASS})
    assert r.status_code == 200, r.text
    tok = r.json().get("access_token")
    assert tok
    return tok


@pytest.fixture(scope="module")
def headers(token):
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


@pytest.fixture(scope="module", autouse=True)
def restore_crypto_nulls(headers):
    """Ensure crypto config restored to nulls after tests."""
    yield
    requests.post(
        f"{BASE_URL}/api/admin/gain-goals/config",
        headers=headers,
        json={"crypto": {"target_net_pnl_usd": None,
                         "maximum_window_drawdown_usd": None}},
    )


def test_get_gain_goals(headers):
    r = requests.get(f"{BASE_URL}/api/admin/gain-goals", headers=headers)
    assert r.status_code == 200
    d = r.json()
    assert "config" in d and "lanes" in d
    assert "equity" in d["lanes"] and "crypto" in d["lanes"]
    eq = d["lanes"]["equity"]
    cr = d["lanes"]["crypto"]
    assert "status" in eq and "label" in eq
    assert "window_start" in eq and "window_end" in eq
    assert eq["time_progress"]["mode"] == "rth_sessions"
    assert cr["time_progress"]["mode"] == "elapsed_time"
    assert "net_realized_pnl_usd" in eq
    assert "resolved_trades" in eq
    assert "minimum_resolved_trades" in eq
    assert d["global_rollup"]["read_only"] is True


def test_post_config_crypto_target(headers):
    r = requests.post(
        f"{BASE_URL}/api/admin/gain-goals/config",
        headers=headers,
        json={"crypto": {"target_net_pnl_usd": 100,
                         "maximum_window_drawdown_usd": 50}},
    )
    assert r.status_code == 200, r.text
    # Re-fetch
    r2 = requests.get(f"{BASE_URL}/api/admin/gain-goals", headers=headers)
    d = r2.json()
    cr = d["lanes"]["crypto"]
    assert cr["status"] == "INSUFFICIENT_SAMPLE", cr
    assert cr["effective_target_usd"] == 100
    assert cr.get("drawdown_limit_usd") == 50


def test_post_config_restore_nulls(headers):
    r = requests.post(
        f"{BASE_URL}/api/admin/gain-goals/config",
        headers=headers,
        json={"crypto": {"target_net_pnl_usd": None,
                         "maximum_window_drawdown_usd": None}},
    )
    assert r.status_code == 200
    r2 = requests.get(f"{BASE_URL}/api/admin/gain-goals", headers=headers)
    cr = r2.json()["lanes"]["crypto"]
    assert cr["status"] == "NO_GOAL", cr


def test_post_config_unknown_key_422(headers):
    r = requests.post(
        f"{BASE_URL}/api/admin/gain-goals/config",
        headers=headers,
        json={"crypto": {"bogus": 1}},
    )
    assert r.status_code == 422, r.status_code


def test_ack_no_breach(headers):
    r = requests.post(f"{BASE_URL}/api/admin/gain-goals/ack",
                      headers=headers, json={"lane": "crypto"})
    assert r.status_code == 200
    d = r.json()
    assert d.get("ok") is False
    assert d.get("reason") == "no_active_breach"


def test_ack_invalid_lane(headers):
    r = requests.post(f"{BASE_URL}/api/admin/gain-goals/ack",
                      headers=headers, json={"lane": "banana"})
    assert r.status_code == 422


def test_evaluate_advances(headers):
    r1 = requests.post(f"{BASE_URL}/api/admin/gain-goals/evaluate",
                       headers=headers)
    assert r1.status_code == 200
    d1 = r1.json()
    ts1 = d1.get("evaluated_at") or d1.get("lanes", {}).get("crypto", {}).get("evaluated_at")
    import time
    time.sleep(1.1)
    r2 = requests.post(f"{BASE_URL}/api/admin/gain-goals/evaluate",
                       headers=headers)
    d2 = r2.json()
    ts2 = d2.get("evaluated_at") or d2.get("lanes", {}).get("crypto", {}).get("evaluated_at")
    assert ts1 and ts2 and ts2 >= ts1, f"{ts1} -> {ts2}"


def test_hotpath_policy_snapshot_has_gain_goal(headers):
    r = requests.get(f"{BASE_URL}/api/admin/hotpath/policy", headers=headers)
    assert r.status_code == 200
    d = r.json()
    # gain_goal must be present somewhere in snapshot
    snap = d.get("snapshot", d)
    assert "gain_goal" in snap or "gain_goal" in d, f"gain_goal missing in {list(d.keys())}"
    gg = snap.get("gain_goal") or d.get("gain_goal")
    assert "block" in gg or "throttle" in gg, gg
