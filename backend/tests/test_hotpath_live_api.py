"""Live API smoke tests for Hot-Path Audit endpoints (ExecutionPolicySnapshot + Intent Queue).

Runs against REACT_APP_BACKEND_URL. Read-only + budget cap/reset (cap restored to null at end).
Does NOT touch trading switches, broker freeze, or intent emission.
"""
import os
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://multi-brain-backbone.preview.emergentagent.com").rstrip("/")
ADMIN_EMAIL = "admin@risedual.io"
ADMIN_PASSWORD = "risedual-admin-2026"


@pytest.fixture(scope="module")
def token():
    r = requests.post(f"{BASE_URL}/api/auth/login",
                      json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
                      timeout=30)
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
    data = r.json()
    tok = data.get("access_token") or data.get("token")
    assert tok, f"no token in {data}"
    return tok


@pytest.fixture(scope="module")
def H(token):
    return {"Authorization": f"Bearer {token}"}


# --- Hotpath policy ---
def test_hotpath_policy(H):
    r = requests.get(f"{BASE_URL}/api/admin/hotpath/policy", headers=H, timeout=30)
    assert r.status_code == 200, r.text
    d = r.json()
    # snapshot fields
    snap = d.get("snapshot", d)
    assert "version" in snap
    assert snap.get("source") == "atlas", f"source={snap.get('source')}"
    refresher = snap.get("refresher") or d.get("refresher") or {}
    assert refresher.get("running") is True, f"refresher={refresher}"
    for k in ("lane_enabled", "master_switch_enabled", "conviction_floor", "cap_daily_usd_effective"):
        assert k in snap, f"missing {k} in snapshot: {list(snap.keys())}"
    ds = d.get("daily_spend") or snap.get("daily_spend") or {}
    for k in ("day", "spent_usd", "bootstrapped"):
        assert k in ds, f"missing {k} in daily_spend: {ds}"
    assert ds["bootstrapped"] is True
    print(f"policy version={snap.get('version')} spent={ds.get('spent_usd')} cap_eff={snap.get('cap_daily_usd_effective')}")


def test_hotpath_policy_refresh(H):
    r1 = requests.get(f"{BASE_URL}/api/admin/hotpath/policy", headers=H, timeout=30).json()
    v0 = (r1.get("snapshot") or r1).get("version")
    r = requests.post(f"{BASE_URL}/api/admin/hotpath/policy/refresh", headers=H, timeout=30)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d.get("ok") is True, d
    v1 = d.get("version") or (d.get("snapshot") or {}).get("version")
    assert v1 is not None and (v0 is None or v1 >= v0), f"version did not advance: {v0}->{v1}"
    dk = d.get("degraded_keys")
    assert dk == [] or dk is None, f"degraded_keys not empty: {dk}"


def test_hotpath_queue(H):
    r = requests.get(f"{BASE_URL}/api/admin/hotpath/queue", headers=H, timeout=30)
    assert r.status_code == 200, r.text
    d = r.json()
    q = d.get("queue", d)
    for k in ("cached", "pending", "by_state", "bootstrapped"):
        assert k in q, f"missing {k}: {list(q.keys())}"
    assert q["bootstrapped"] is True
    assert isinstance(q["by_state"], dict)


# --- Risk budget ---
def test_risk_budget_status(H):
    r = requests.get(f"{BASE_URL}/api/admin/risk/budget", headers=H, timeout=30)
    assert r.status_code == 200, r.text
    d = r.json()
    for k in ("spent_today_usd", "cap_daily_usd", "remaining_usd"):
        assert k in d, f"missing {k}: {d}"
        assert isinstance(d[k], (int, float)), f"{k} not numeric: {d[k]}"
    expected_remaining = max(0, d["cap_daily_usd"] - d["spent_today_usd"])
    assert abs(d["remaining_usd"] - expected_remaining) < 0.01, f"remaining mismatch: {d}"


def test_risk_budget_cap_set_and_revert(H):
    # set cap to 750
    r = requests.post(f"{BASE_URL}/api/admin/risk/budget/cap", headers=H,
                      json={"cap_daily_usd": 750}, timeout=30)
    assert r.status_code == 200, r.text
    d = r.json()
    # response should reflect 750 somewhere
    cap_seen = d.get("cap_daily_usd") or (d.get("snapshot") or {}).get("cap_daily_usd_effective") or d.get("cap_daily_usd_override")
    assert cap_seen == 750, f"expected 750 in response, got {d}"

    # verify snapshot shows override=750
    pol = requests.get(f"{BASE_URL}/api/admin/hotpath/policy", headers=H, timeout=30).json()
    snap = pol.get("snapshot") or pol
    override = snap.get("cap_daily_usd_override")
    assert override == 750, f"cap_daily_usd_override expected 750, got {override} full={snap}"

    # revert to null
    r2 = requests.post(f"{BASE_URL}/api/admin/risk/budget/cap", headers=H,
                       json={"cap_daily_usd": None}, timeout=30)
    assert r2.status_code == 200, r2.text
    d2 = r2.json()
    # after revert override should be None; effective should be 1000 (env default)
    pol2 = requests.get(f"{BASE_URL}/api/admin/hotpath/policy", headers=H, timeout=30).json()
    snap2 = pol2.get("snapshot") or pol2
    assert snap2.get("cap_daily_usd_override") in (None, 0) or snap2.get("cap_daily_usd_override") is None, f"override not reverted: {snap2.get('cap_daily_usd_override')}"
    assert snap2.get("cap_daily_usd_effective") == 1000, f"effective not 1000 after revert: {snap2.get('cap_daily_usd_effective')}"


def test_risk_budget_reset(H):
    r = requests.post(f"{BASE_URL}/api/admin/risk/budget/reset", headers=H, timeout=30)
    assert r.status_code == 200, r.text
    d = r.json()
    spent = d.get("spent_today_usd")
    assert spent is not None
    # allow small non-zero if live crypto executions fire concurrently
    assert spent < 50.0, f"spent not near zero after reset: {spent}"
    # follow-up GET
    r2 = requests.get(f"{BASE_URL}/api/admin/risk/budget", headers=H, timeout=30).json()
    assert r2["spent_today_usd"] < 50.0, f"GET after reset shows: {r2['spent_today_usd']}"


# --- Auto-router / regression ---
def test_auto_router_status(H):
    r = requests.get(f"{BASE_URL}/api/admin/auto-router/status", headers=H, timeout=30)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d.get("task_alive") is True, f"task_alive={d.get('task_alive')} full={d}"
    assert "intent_queue_source" in d, f"missing intent_queue_source: {list(d.keys())}"
    assert d.get("last_tick_error") in (None, "", "null"), f"last_tick_error={d.get('last_tick_error')}"


def test_hotpath_outbox(H):
    r = requests.get(f"{BASE_URL}/api/admin/hotpath/outbox", headers=H, timeout=30)
    assert r.status_code == 200, r.text
    d = r.json()
    # basic sanity — has some outbox status + dead_letters key
    assert "dead_letters" in d or "outbox" in d or "cached" in d, f"unexpected shape: {list(d.keys())}"


def test_scanner_status(H):
    r = requests.get(f"{BASE_URL}/api/admin/scanner", headers=H, timeout=30)
    assert r.status_code == 200, r.text


def test_expectancy_summary(H):
    r = requests.get(f"{BASE_URL}/api/admin/expectancy", headers=H, timeout=30)
    assert r.status_code == 200, r.text


def test_gto_status_intents_age_bound(H):
    r = requests.get(f"{BASE_URL}/api/admin/runtime/gto/status", headers=H, timeout=30)
    assert r.status_code == 200, r.text
    d = r.json()
    payload = d.get("payload") or d
    intents = payload.get("intents") or {}
    lts = intents.get("latest_ts")
    age = intents.get("latest_age_s")
    # both null or both set
    if lts is None:
        assert age is None, f"latest_ts null but age={age}"
    else:
        assert age is not None, f"latest_ts set ({lts}) but age is None"
        assert age <= 48 * 3600, f"latest_age_s {age} exceeds 48h bound"
