"""Integration tests for the Position Monitor REST + Risk Guards REST +
per-lane Intents + Brain × Lane Policy (this iteration's P0 / P1 scope).

Hits the public preview URL using the operator JWT obtained from
/api/auth/login. Read-only side effects on Mongo are bounded to test
data (brain-lane-policy entries + a couple of intents); a teardown
restores the brain-lane-policy default at the end.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://multi-brain-backbone.preview.emergentagent.com").rstrip("/")
ADMIN_EMAIL = "admin@risedual.io"
ADMIN_PASSWORD = "risedual-admin-2026"


@pytest.fixture(scope="session")
def token() -> str:
    r = requests.post(f"{BASE_URL}/api/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}, timeout=15)
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
    body = r.json()
    tok = body.get("access_token")
    assert tok, "no access_token in login response"
    return tok


@pytest.fixture(scope="session")
def client(token: str) -> requests.Session:
    s = requests.Session()
    s.headers.update({"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    return s


# ───────── Position Monitor status / run-once / recent-evaluations ─────────

def test_monitor_status(client: requests.Session):
    r = client.get(f"{BASE_URL}/api/admin/risk/monitor/status", timeout=10)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("running") is True
    pr = body.get("priority")
    assert pr == ["stop_loss", "take_profit", "trailing_stop", "max_hold_time"], pr
    cfg = body.get("config") or {}
    # all four tuneables present
    for k in ("stop_loss_pct", "take_profit_pct", "trail_pct", "max_hold_minutes"):
        assert k in cfg, f"config missing {k}: {cfg}"


def test_monitor_run_once(client: requests.Session):
    r = client.post(f"{BASE_URL}/api/admin/risk/monitor/run-once", timeout=20)
    assert r.status_code == 200, r.text
    body = r.json()
    # Backend returns {summary:{...}, results:[...]}
    summary = body.get("summary", body)
    for k in ("open_positions", "evaluated", "actions_taken", "errors"):
        assert k in summary, f"summary missing key {k}: {body}"
    assert isinstance(summary["open_positions"], int)
    assert isinstance(summary["evaluated"], int)


def test_monitor_recent_evaluations(client: requests.Session):
    r = client.get(f"{BASE_URL}/api/admin/risk/monitor/recent-evaluations", timeout=10)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "items" in body and "count" in body
    assert isinstance(body["items"], list)
    assert body["count"] == len(body["items"])


# ───────── Pure-math guard endpoints ─────────

def test_stop_loss_long_triggers_close(client: requests.Session):
    r = client.post(f"{BASE_URL}/api/admin/risk/stop-loss/evaluate",
                    json={"side": "LONG", "entry_price": 100, "current_price": 97, "stop_loss_pct": 2}, timeout=10)
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["action"] == "CLOSE"
    assert b["guard"] == "stop_loss"


def test_stop_loss_long_holds(client: requests.Session):
    r = client.post(f"{BASE_URL}/api/admin/risk/stop-loss/evaluate",
                    json={"side": "LONG", "entry_price": 100, "current_price": 99, "stop_loss_pct": 2}, timeout=10)
    assert r.status_code == 200
    assert r.json()["action"] == "HOLD"


def test_trailing_stop_inactive(client: requests.Session):
    # previous_peak < entry_price for LONG → inactive → HOLD
    r = client.post(f"{BASE_URL}/api/admin/risk/trailing-stop/evaluate",
                    json={"side": "LONG", "entry_price": 100, "current_price": 99,
                          "previous_peak": 99.5, "trail_pct": 1.5, "activate_after_pct": 1.0}, timeout=10)
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["action"] == "HOLD"
    assert "inactive" in (b.get("reason") or "").lower()


def test_trailing_stop_triggers_close(client: requests.Session):
    r = client.post(f"{BASE_URL}/api/admin/risk/trailing-stop/evaluate",
                    json={"side": "LONG", "entry_price": 100, "current_price": 103,
                          "previous_peak": 105, "trail_pct": 1.5, "activate_after_pct": 1.0}, timeout=10)
    assert r.status_code == 200, r.text
    assert r.json()["action"] == "CLOSE"


def test_max_hold_time_close_old(client: requests.Session):
    opened = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    r = client.post(f"{BASE_URL}/api/admin/risk/max-hold-time/evaluate",
                    json={"opened_at": opened, "max_hold_minutes": 60}, timeout=10)
    assert r.status_code == 200, r.text
    assert r.json()["action"] == "CLOSE"


def test_max_hold_time_holds_fresh(client: requests.Session):
    opened = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    r = client.post(f"{BASE_URL}/api/admin/risk/max-hold-time/evaluate",
                    json={"opened_at": opened, "max_hold_minutes": 60}, timeout=10)
    assert r.status_code == 200
    assert r.json()["action"] == "HOLD"


# ───────── Per-lane Intents ─────────

def test_intent_equity_creates(client: requests.Session):
    body = {
        "stack": "alpha", "action": "BUY", "symbol": "NVDA",
        "confidence": 0.7, "risk_multiplier": 0.1,
        "rationale": "test intent equity NVDA",
    }
    r = client.post(f"{BASE_URL}/api/admin/intents/equity", json=body, timeout=15)
    assert r.status_code in (200, 201), f"equity intent failed: {r.status_code} {r.text}"
    data = r.json()
    # response shape should at least carry the intent or an id
    assert isinstance(data, dict)


def test_intent_crypto_creates(client: requests.Session):
    body = {
        "stack": "redeye", "action": "BUY", "symbol": "BTC/USD",
        "confidence": 0.6, "risk_multiplier": 0.1,
        "rationale": "test intent crypto BTC",
    }
    r = client.post(f"{BASE_URL}/api/admin/intents/crypto", json=body, timeout=15)
    assert r.status_code in (200, 201), f"crypto intent failed: {r.status_code} {r.text}"


def test_intent_crypto_lane_pin_rejects_equity(client: requests.Session):
    body = {
        "stack": "redeye", "action": "BUY", "symbol": "BTC/USD",
        "confidence": 0.6, "risk_multiplier": 0.1,
        "rationale": "lane pin test",
        "lane": "equity",  # mismatched
    }
    r = client.post(f"{BASE_URL}/api/admin/intents/crypto", json=body, timeout=15)
    assert r.status_code == 400, f"expected 400 from lane-pin, got {r.status_code} {r.text}"


# ───────── Brain × Lane Policy ─────────

def test_brain_lane_policy_full_cycle(client: requests.Session):
    # GET
    r = client.get(f"{BASE_URL}/api/admin/brain-lane-policy", timeout=10)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "items" in body
    eff = body.get("effective") or body.get("matrix")
    assert eff is not None, f"missing effective matrix: {body}"

    # Camaro/crypto should be muted explicitly (per spec seed)
    # Accept either format: nested dict {brain: {lane: bool}} OR list of cells
    def _is_allowed(brain: str, lane: str) -> bool:
        if isinstance(eff, dict):
            v = eff.get(brain, {}).get(lane)
            if isinstance(v, dict):
                return bool(v.get("allowed", True))
            return bool(v) if v is not None else True
        if isinstance(eff, list):
            for cell in eff:
                if cell.get("brain") == brain and cell.get("lane") == lane:
                    return bool(cell.get("allowed", True))
        return True

    assert _is_allowed("camaro", "crypto") is False, f"expected camaro/crypto muted: {eff}"

    # POST mute alpha/crypto
    r = client.post(f"{BASE_URL}/api/admin/brain-lane-policy",
                    json={"brain": "alpha", "lane": "crypto", "allowed": False}, timeout=10)
    assert r.status_code in (200, 201), r.text

    # Verify via GET
    r = client.get(f"{BASE_URL}/api/admin/brain-lane-policy", timeout=10)
    assert r.status_code == 200
    body2 = r.json()
    eff2 = body2.get("effective") or body2.get("matrix")

    def _allowed2(brain, lane):
        if isinstance(eff2, dict):
            v = eff2.get(brain, {}).get(lane)
            if isinstance(v, dict):
                return bool(v.get("allowed", True))
            return bool(v) if v is not None else True
        for cell in eff2 or []:
            if cell.get("brain") == brain and cell.get("lane") == lane:
                return bool(cell.get("allowed", True))
        return True

    assert _allowed2("alpha", "crypto") is False

    # DELETE restores default-allow
    r = client.delete(f"{BASE_URL}/api/admin/brain-lane-policy/alpha/crypto", timeout=10)
    assert r.status_code in (200, 204), r.text

    # Verify final state
    r = client.get(f"{BASE_URL}/api/admin/brain-lane-policy", timeout=10)
    eff3 = (r.json() or {}).get("effective") or (r.json() or {}).get("matrix")

    def _allowed3(brain, lane):
        if isinstance(eff3, dict):
            v = eff3.get(brain, {}).get(lane)
            if isinstance(v, dict):
                return bool(v.get("allowed", True))
            return bool(v) if v is not None else True
        for cell in eff3 or []:
            if cell.get("brain") == brain and cell.get("lane") == lane:
                return bool(cell.get("allowed", True))
        return True

    assert _allowed3("alpha", "crypto") is True, "DELETE did not restore default-allow"
