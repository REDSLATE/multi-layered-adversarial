"""Backend tests for Ignition Watch + Missed-Entry Ledger + Momentum Scanner P0 fix."""
import os
import time
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
    assert tok, f"no access_token in {data}"
    return tok


@pytest.fixture(scope="module")
def client(token):
    s = requests.Session()
    s.headers.update({"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    return s


# ---- 1. Login already tested via fixture ----
def test_login_returns_access_token(token):
    assert isinstance(token, str) and len(token) > 10


# ---- 2. Momentum scanner GET ----
def test_momentum_scanner_get(client):
    r = client.get(f"{BASE_URL}/api/admin/momentum-scanner", timeout=30)
    assert r.status_code == 200, r.text
    data = r.json()
    cfg = data.get("config") or {}
    assert cfg.get("ignition_enabled") is True, f"ignition_enabled expected True: {cfg}"
    assert cfg.get("ignition_top_n") == 5, f"ignition_top_n expected 5: {cfg}"
    assert cfg.get("ignition_min_vol_usd_min") == 10000, f"ignition_min_vol_usd_min: {cfg}"
    state = data.get("state") or {}
    last_run = state.get("last_run")
    assert last_run, f"last_run missing: {state}"
    # Freshness: within last ~3 minutes
    import datetime as dt
    try:
        ts = dt.datetime.fromisoformat(last_run.replace("Z", "+00:00"))
    except Exception:
        pytest.fail(f"Unable to parse last_run={last_run}")
    now = dt.datetime.now(dt.timezone.utc)
    age = (now - ts).total_seconds()
    assert age < 240, f"last_run stale: age={age}s last_run={last_run}"
    # Candidates should have 'origin' field
    candidates = state.get("candidates") or []
    if candidates:
        for c in candidates:
            assert "origin" in c, f"candidate missing origin: {c}"
    else:
        # Empty is allowed on flat tape per note
        print("No candidates present — allowed if empty tape")


# ---- 3. POST scanner update knob + restore ----
def test_momentum_scanner_post_update(client):
    r = client.post(f"{BASE_URL}/api/admin/momentum-scanner",
                    json={"ignition_top_n": 6}, timeout=30)
    assert r.status_code == 200, r.text
    data = r.json()
    cfg = data.get("config") or {}
    assert cfg.get("ignition_top_n") == 6, f"knob not echoed: {cfg}"
    # Restore
    r2 = client.post(f"{BASE_URL}/api/admin/momentum-scanner",
                     json={"ignition_top_n": 5}, timeout=30)
    assert r2.status_code == 200
    assert (r2.json().get("config") or {}).get("ignition_top_n") == 5


# ---- 4. Missed-entries GET ----
def test_missed_entries_get(client):
    r = client.get(f"{BASE_URL}/api/admin/missed-entries", timeout=30)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data.get("ok") is True, data
    cfg = data.get("config") or {}
    assert "enabled" in cfg and "horizon_h" in cfg and "max_eval_per_cycle" in cfg, cfg
    assert "by_reason" in data
    recent = data.get("recent") or []
    demo = [r for r in recent if r.get("intent_id") == "validation-missed-demo-1"]
    assert demo, f"validation-missed-demo-1 not found in recent: {[x.get('intent_id') for x in recent]}"
    row = demo[0]
    assert row.get("symbol") == "BTC/USD", row
    assert row.get("outcome") == "expired", row
    peak = float(row.get("peak_pct") or 0)
    end = float(row.get("end_pct") or 0)
    assert abs(peak - 0.575) < 0.15, f"peak_pct ~0.575 expected, got {peak}"
    assert abs(end - 0.494) < 0.15, f"end_pct ~0.494 expected, got {end}"


# ---- 5. POST missed-entries horizon update + restore ----
def test_missed_entries_post_update(client):
    r = client.post(f"{BASE_URL}/api/admin/missed-entries", json={"horizon_h": 6}, timeout=30)
    assert r.status_code == 200, r.text
    cfg = (r.json().get("config") or {})
    assert cfg.get("horizon_h") == 6, cfg
    r2 = client.post(f"{BASE_URL}/api/admin/missed-entries", json={"horizon_h": 4}, timeout=30)
    assert r2.status_code == 200
    assert (r2.json().get("config") or {}).get("horizon_h") == 4


# ---- 6. run-once idempotent ----
def test_missed_entries_run_once_idempotent(client):
    r = client.post(f"{BASE_URL}/api/admin/missed-entries/run-once", timeout=60)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data.get("ok") is True, data
    stats = data.get("stats") or {}
    for k in ("scanned", "evaluated", "no_data"):
        assert k in stats, f"missing key {k} in {stats}"
    assert stats["evaluated"] == 0, f"expected 0 evaluated on rerun, got {stats}"


# ---- 7. Backend logs no NameError ----
def test_no_crypto_scan_cap_nameerror():
    # Only tail last chunk for speed
    logs = ""
    for p in ("/var/log/supervisor/backend.err.log", "/var/log/supervisor/backend.out.log"):
        try:
            with open(p, "r") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 200_000))
                logs += f.read()
        except Exception:
            pass
    # Only fail on actual NameError for CRYPTO_SCAN_CAP (not any historic mention)
    assert "NameError" not in logs or "CRYPTO_SCAN_CAP" not in logs.split("NameError")[-1][:500], \
        "NameError: CRYPTO_SCAN_CAP found in recent backend logs"
    # Look for recent momentum_scanner loop errors
    recent_tail = logs[-50000:]
    assert "momentum_scanner loop error" not in recent_tail.lower(), \
        "momentum_scanner loop error in recent logs"


# ---- 8. Regression buy-eligibility ----
def test_buy_eligibility_regression(client):
    r = client.get(f"{BASE_URL}/api/admin/universe/buy-eligibility", timeout=30)
    assert r.status_code == 200, r.text
    data = r.json()
    # search top-level for max_notional_usd
    def find_key(obj, key):
        if isinstance(obj, dict):
            if key in obj:
                return obj[key]
            for v in obj.values():
                res = find_key(v, key)
                if res is not None:
                    return res
        elif isinstance(obj, list):
            for v in obj:
                res = find_key(v, key)
                if res is not None:
                    return res
        return None
    mn = find_key(data, "max_notional_usd")
    assert mn is not None and float(mn) == 5.0, f"max_notional_usd expected 5.0, got {mn}"

    r2 = client.get(f"{BASE_URL}/api/admin/universe/buy-eligibility/probe",
                    params={"symbol": "BTC/USD"}, timeout=30)
    assert r2.status_code == 200, r2.text
    data2 = r2.json()
    op = find_key(data2, "operator_pin")
    # operator_pin may be a nested dict OR the string reason on the receipt
    if op is None:
        # Check for receipt.reason == 'operator_pin' with notional_cap_usd
        reason = find_key(data2, "reason")
        assert reason == "operator_pin", f"operator_pin not found: {data2}"
        nc = find_key(data2, "notional_cap_usd")
    else:
        nc = find_key(op, "notional_cap_usd") if isinstance(op, dict) else find_key(data2, "notional_cap_usd")
    assert nc is not None and float(nc) == 5.0, f"notional_cap_usd expected 5.0, got {nc}"
