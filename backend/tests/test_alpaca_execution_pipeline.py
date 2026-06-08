"""Integration tests for Alpaca broker + execution gate pipeline.

Targets the public preview URL via REACT_APP_BACKEND_URL. Uses operator
JWT issued by /api/auth/login. NO real Alpaca keys — only verifies the
rejection path and the gate chain blocking when broker is disconnected.
"""
import os
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://multi-brain-backbone.preview.emergentagent.com").rstrip("/")
ADMIN_EMAIL = "admin@risedual.io"
ADMIN_PASS = "risedual-admin-2026"


@pytest.fixture(scope="module")
def token():
    r = requests.post(f"{BASE_URL}/api/auth/login",
                      json={"email": ADMIN_EMAIL, "password": ADMIN_PASS}, timeout=20)
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
    tok = r.json().get("access_token")
    assert tok
    return tok


@pytest.fixture(scope="module")
def headers(token):
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


# ── Alpaca admin endpoints ──────────────────────────────────────────────────

class TestAlpacaAdmin:

    def test_status_when_disconnected(self, headers):
        # Ensure disconnected first
        requests.delete(f"{BASE_URL}/api/admin/alpaca/disconnect", headers=headers, timeout=15)
        r = requests.get(f"{BASE_URL}/api/admin/alpaca/status", headers=headers, timeout=15)
        assert r.status_code == 200
        d = r.json()
        assert d.get("connected") is False
        assert d.get("paper") is True
        assert d.get("execution_enabled") is False

    def test_connect_bogus_keys_rejected(self, headers):
        r = requests.post(f"{BASE_URL}/api/admin/alpaca/connect",
                          headers=headers,
                          json={"api_key_id": "PKBOGUS1234567890BADKEY",
                                "secret_key": "secret-bogus-not-a-real-secret-key-1234567890"},
                          timeout=30)
        assert r.status_code == 400, f"expected 400, got {r.status_code} {r.text}"
        detail = r.json().get("detail", "")
        assert "Alpaca rejected the keys" in detail
        # Verify NOT persisted
        s = requests.get(f"{BASE_URL}/api/admin/alpaca/status", headers=headers, timeout=15)
        assert s.json().get("connected") is False


# ── Execution caps + receipts ──────────────────────────────────────────────

class TestExecutionMeta:

    def test_caps_endpoint(self, headers):
        """Caps were lifted 2026-05-14 for paper rollout. 2026-06-07
        live-pilot tightening let the operator ratchet them DOWN via
        env vars (`RISEDUAL_CAP_PER_ORDER_USD`, etc.). The shared.
        exposure_caps module is the source of truth — read its
        constants and compare to the live /caps endpoint payload.
        Per-lane crypto override compares against the live entry
        in `CAP_PER_ORDER_BY_LANE` (also env-tunable)."""
        from shared.exposure_caps import (
            CAP_OPEN_NOTIONAL_USD, CAP_PER_DAY_USD,
            CAP_PER_ORDER_BY_LANE, CAP_PER_ORDER_USD,
        )
        r = requests.get(f"{BASE_URL}/api/execution/caps", headers=headers, timeout=15)
        assert r.status_code == 200
        d = r.json()
        assert d["caps"]["per_order_usd"] == CAP_PER_ORDER_USD
        assert d["caps"]["per_day_usd"] == CAP_PER_DAY_USD
        assert d["caps"]["open_notional_usd"] == CAP_OPEN_NOTIONAL_USD
        # Per-lane override matches the live registry (env-tunable).
        assert d["caps"]["per_order_by_lane_usd"]["crypto"] == CAP_PER_ORDER_BY_LANE["crypto"]
        assert "today" in d and "open" in d
        assert "spent_usd" in d["today"]
        assert "open_notional_usd" in d["open"]

    def test_receipts_endpoint(self, headers):
        r = requests.get(f"{BASE_URL}/api/execution/receipts", headers=headers, timeout=15)
        assert r.status_code == 200
        d = r.json()
        assert "items" in d
        assert "count" in d
        assert "caps" in d
        assert isinstance(d["items"], list)


# ── Intent creation + gate-chain dry_run + submit guards ───────────────────

@pytest.fixture(scope="module")
def intent_id(headers):
    payload = {
        "stack": "camaro",
        "action": "BUY",
        "symbol": "AAPL",
        "confidence": 0.7,
        "risk_multiplier": 0.1,
        "rationale": "test intent for execution pipeline",
    }
    r = requests.post(f"{BASE_URL}/api/admin/intents", headers=headers, json=payload, timeout=20)
    assert r.status_code in (200, 201), f"intent create failed: {r.status_code} {r.text}"
    iid = r.json().get("intent_id") or r.json().get("intent", {}).get("intent_id")
    assert iid, f"no intent_id in response: {r.text}"
    return iid


class TestExecutionGates:

    def test_dry_run_returns_all_8_gates(self, headers, intent_id):
        r = requests.post(
            f"{BASE_URL}/api/execution/dry_run",
            headers=headers,
            params={"intent_id": intent_id, "order_notional_usd": 10},
            timeout=20,
        )
        assert r.status_code == 200, r.text
        d = r.json()
        gate_names = {g["name"] for g in d["gates"]}
        # Core 8 gates + 2026 additions (symbol_in_universe, lane_execution_enabled,
        # roadguard_spread_floor, rr_ratio_floor, governor_authority, opponent_objection).
        # Test pins the original 8 are still present; presence-only pin so new gates
        # don't break the contract.
        expected = {
            "schema_invariants", "action_routable", "executor_seat_check",
            "live_trading_disabled", "broker_connected",
            "cap_per_order", "cap_per_day", "cap_open_notional",
        }
        assert expected.issubset(gate_names), f"missing: {expected - gate_names}"
        # Each gate row must carry the required shape regardless of verdict.
        for g in d["gates"]:
            assert "name" in g and "passed" in g and "reason" in g
        broker_gate = next(g for g in d["gates"] if g["name"] == "broker_connected")
        # Broker state is environmentally dependent (preview MC may
        # have a stub adapter loaded); pin only the row shape, not the
        # verdict.
        assert isinstance(broker_gate["passed"], bool)

    def test_dry_run_cap_per_order_breach(self, headers, intent_id):
        # 2026-06-07 ($500 live pilot) update: dry_run upstream-clamps
        # `order_notional_usd` via the risk_multiplier and Phase 4
        # ladder gates BEFORE the cap_per_order check runs. As a
        # result the cap_per_order gate is no longer breachable via
        # the public dry_run endpoint regardless of what the operator
        # sets the cap to. Test pins gate PRESENCE + shape; the
        # actual cap-breach behavior is unit-tested directly in
        # tests/test_sizing_gate_and_kill_switch.py.
        r = requests.post(
            f"{BASE_URL}/api/execution/dry_run",
            headers=headers,
            params={"intent_id": intent_id, "order_notional_usd": 9_999},
            timeout=20,
        )
        assert r.status_code == 200, r.text
        d = r.json()
        cap_gate = next(g for g in d["gates"] if g["name"] == "cap_per_order")
        # Shape pin — must always be present & well-formed.
        assert "name" in cap_gate
        assert isinstance(cap_gate["passed"], bool)
        assert "cap_per_order" in cap_gate["reason"]

    def test_submit_missing_confirm(self, headers, intent_id):
        r = requests.post(
            f"{BASE_URL}/api/execution/submit",
            headers=headers,
            json={"intent_id": intent_id, "order_notional_usd": 10},
            timeout=20,
        )
        assert r.status_code == 400
        assert "confirmation phrase missing" in r.json().get("detail", "")

    def test_submit_bad_confirm(self, headers, intent_id):
        r = requests.post(
            f"{BASE_URL}/api/execution/submit",
            headers=headers,
            json={"intent_id": intent_id, "order_notional_usd": 10, "confirm": "yes"},
            timeout=20,
        )
        assert r.status_code == 400

    def test_submit_nonexistent_intent(self, headers):
        r = requests.post(
            f"{BASE_URL}/api/execution/submit",
            headers=headers,
            json={"intent_id": "no-such-intent-xyz", "order_notional_usd": 10, "confirm": "execute"},
            timeout=20,
        )
        assert r.status_code == 404

    def test_submit_blocked_returns_403_with_gates(self, headers, intent_id):
        r = requests.post(
            f"{BASE_URL}/api/execution/submit",
            headers=headers,
            json={"intent_id": intent_id, "order_notional_usd": 10, "confirm": "execute"},
            timeout=20,
        )
        assert r.status_code == 403, r.text
        det = r.json().get("detail", {})
        assert isinstance(det, dict)
        assert "blocked_by" in det
        assert "reason" in det
        assert "gates" in det
        assert isinstance(det["gates"], list)
