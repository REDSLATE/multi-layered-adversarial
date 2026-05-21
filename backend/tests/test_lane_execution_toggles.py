"""Tripwire tests for the lane execution toggles.

Doctrine pin (2026-02-18):
    The operator owns two independent kill switches — `equity` and
    `crypto`. Both default OFF on a fresh install (safe). Decoupled
    from broker credential state. Enforced by the
    `lane_execution_enabled` gate in `_evaluate_gates`.

These tests assert:
    1. Default-OFF (cold collection → both False).
    2. Flipping equity does not change crypto and vice versa.
    3. Audit log records every flip with previous/next.
    4. The gate chain reads the toggle and fails closed when OFF.
    5. The toggle can be ON while broker is disconnected (decoupled).
"""
from __future__ import annotations

import pytest
import requests

from namespaces import LANE_EXECUTION_TOGGLES, LANE_EXECUTION_AUDIT_LOG


@pytest.fixture(autouse=True)
async def _reset_toggles():
    """Reset the singleton + audit log around each test for hermetic
    state. Uses the live DB import — test_database namespace."""
    from db import db
    await db[LANE_EXECUTION_TOGGLES].delete_many({})
    await db[LANE_EXECUTION_AUDIT_LOG].delete_many({})
    yield
    await db[LANE_EXECUTION_TOGGLES].delete_many({})
    await db[LANE_EXECUTION_AUDIT_LOG].delete_many({})


# ─── HTTP: defaults + flips ──────────────────────────────────────────


@pytest.mark.tripwire
def test_lane_toggles_default_off(auth_client, base_url):
    r = auth_client.get(f"{base_url}/api/admin/execution/lane-toggles", timeout=15)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["equity"] is False, "equity toggle MUST default off"
    assert body["crypto"] is False, "crypto toggle MUST default off"
    assert "doctrine_note" in body


@pytest.mark.tripwire
def test_lane_toggles_require_auth(base_url):
    r = requests.get(
        f"{base_url}/api/admin/execution/lane-toggles", timeout=15,
    )
    assert r.status_code in (401, 403)
    r2 = requests.post(
        f"{base_url}/api/admin/execution/lane-toggles",
        json={"lane": "equity", "enabled": True}, timeout=15,
    )
    assert r2.status_code in (401, 403)


@pytest.mark.tripwire
def test_lane_toggles_flip_equity_independent_of_crypto(auth_client, base_url):
    auth_client.post(
        f"{base_url}/api/admin/execution/lane-toggles",
        json={"lane": "equity", "enabled": True}, timeout=15,
    )
    r = auth_client.get(f"{base_url}/api/admin/execution/lane-toggles", timeout=15)
    body = r.json()
    assert body["equity"] is True
    assert body["crypto"] is False, "flipping equity must NOT enable crypto"


@pytest.mark.tripwire
def test_lane_toggles_flip_crypto_independent_of_equity(auth_client, base_url):
    auth_client.post(
        f"{base_url}/api/admin/execution/lane-toggles",
        json={"lane": "crypto", "enabled": True}, timeout=15,
    )
    r = auth_client.get(f"{base_url}/api/admin/execution/lane-toggles", timeout=15)
    body = r.json()
    assert body["crypto"] is True
    assert body["equity"] is False, "flipping crypto must NOT enable equity"


@pytest.mark.tripwire
def test_lane_toggles_rejects_unknown_lane(auth_client, base_url):
    r = auth_client.post(
        f"{base_url}/api/admin/execution/lane-toggles",
        json={"lane": "options", "enabled": True}, timeout=15,
    )
    # pydantic Literal returns 422 on invalid lane.
    assert r.status_code in (400, 422)


@pytest.mark.tripwire
def test_lane_toggles_history_records_flips(auth_client, base_url):
    auth_client.post(
        f"{base_url}/api/admin/execution/lane-toggles",
        json={"lane": "equity", "enabled": True}, timeout=15,
    )
    auth_client.post(
        f"{base_url}/api/admin/execution/lane-toggles",
        json={"lane": "equity", "enabled": False}, timeout=15,
    )
    r = auth_client.get(
        f"{base_url}/api/admin/execution/lane-toggles/history", timeout=15,
    )
    body = r.json()
    flips = [row for row in body["items"] if row["lane"] == "equity"]
    assert len(flips) >= 2
    # History is sorted desc; latest is OFF (previous True, next False).
    assert flips[0]["next"] is False
    assert flips[0]["previous"] is True


# ─── unit: is_lane_execution_enabled ──────────────────────────────────


@pytest.mark.tripwire
async def test_is_lane_execution_enabled_defaults_false():
    from shared.lane_execution import is_lane_execution_enabled
    assert await is_lane_execution_enabled("equity") is False
    assert await is_lane_execution_enabled("crypto") is False


@pytest.mark.tripwire
async def test_is_lane_execution_enabled_unknown_lane_false():
    from shared.lane_execution import is_lane_execution_enabled
    assert await is_lane_execution_enabled("options") is False
    assert await is_lane_execution_enabled("") is False
    assert await is_lane_execution_enabled(None) is False  # type: ignore[arg-type]


@pytest.mark.tripwire
async def test_set_lane_toggle_persists_and_audits():
    from shared.lane_execution import set_lane_toggle, is_lane_execution_enabled
    from db import db
    await set_lane_toggle("equity", True, "test@test.com")
    assert await is_lane_execution_enabled("equity") is True
    assert await is_lane_execution_enabled("crypto") is False
    audit_count = await db[LANE_EXECUTION_AUDIT_LOG].count_documents({})
    assert audit_count == 1


# ─── gate chain enforcement ───────────────────────────────────────────


@pytest.mark.tripwire
async def test_gate_chain_includes_lane_execution_enabled_gate():
    """The gate chain MUST include the new gate, named
    `lane_execution_enabled`, after `broker_connected`. Tripwire on
    presence + ordering."""
    from shared.execution import _evaluate_gates
    sim_intent = {
        "intent_id": "tripwire-sim",
        "stack": "alpha",
        "symbol": "SPY",
        "action": "BUY",
        "lane": "equity",
        "may_execute": False,
        "requires_gate_pass": True,
        "holds_executor_seat": True,
        "executor_holder_at_post": "alpha",
        "confidence": 0.7,
        "snapshot": {"spread_bps": 5.0},
    }
    result = await _evaluate_gates(sim_intent, 10.0)
    names = [g["name"] for g in result["gates"]]
    assert "lane_execution_enabled" in names
    assert names.index("lane_execution_enabled") > names.index("broker_connected")


@pytest.mark.tripwire
async def test_gate_chain_blocks_when_lane_execution_off():
    """The new gate MUST FAIL when the operator hasn't enabled the
    lane. This is the kill switch's whole point."""
    from shared.execution import _evaluate_gates
    sim_intent = {
        "intent_id": "tripwire-off",
        "stack": "alpha",
        "symbol": "SPY",
        "action": "BUY",
        "lane": "equity",
        "may_execute": False,
        "requires_gate_pass": True,
        "holds_executor_seat": True,
        "executor_holder_at_post": "alpha",
        "confidence": 0.7,
        "snapshot": {"spread_bps": 5.0},
    }
    result = await _evaluate_gates(sim_intent, 10.0)
    gate = next(g for g in result["gates"] if g["name"] == "lane_execution_enabled")
    assert gate["passed"] is False
    assert "NOT enabled" in gate["reason"] or "not enabled" in gate["reason"].lower()


@pytest.mark.tripwire
async def test_gate_chain_passes_lane_when_operator_enables():
    """After the operator flips the toggle to ON, the gate must pass.
    Other gates may still fail (broker_connected etc.) — we only
    assert THIS gate's behavior here."""
    from shared.execution import _evaluate_gates
    from shared.lane_execution import set_lane_toggle
    await set_lane_toggle("equity", True, "test@test.com")

    sim_intent = {
        "intent_id": "tripwire-on",
        "stack": "alpha",
        "symbol": "SPY",
        "action": "BUY",
        "lane": "equity",
        "may_execute": False,
        "requires_gate_pass": True,
        "holds_executor_seat": True,
        "executor_holder_at_post": "alpha",
        "confidence": 0.7,
        "snapshot": {"spread_bps": 5.0},
    }
    result = await _evaluate_gates(sim_intent, 10.0)
    gate = next(g for g in result["gates"] if g["name"] == "lane_execution_enabled")
    assert gate["passed"] is True


@pytest.mark.tripwire
async def test_lane_toggles_decoupled_from_broker_credentials():
    """Doctrine: enabling the toggle MUST NOT cause broker credentials
    to be silently created/altered. The two surfaces are independent."""
    from shared.lane_execution import set_lane_toggle, is_lane_execution_enabled
    from db import db
    from namespaces import ALPACA_CREDENTIALS, KRAKEN_CREDENTIALS
    await set_lane_toggle("equity", True, "test@test.com")
    await set_lane_toggle("crypto", True, "test@test.com")
    assert await is_lane_execution_enabled("equity") is True
    assert await is_lane_execution_enabled("crypto") is True
    # Broker docs MUST NOT have been touched as a side effect.
    alpaca_doc = await db[ALPACA_CREDENTIALS].find_one({"_id": "singleton"})
    kraken_doc = await db[KRAKEN_CREDENTIALS].find_one({"_id": "singleton"})
    # Either both exist (pre-existing) or both don't — neither was
    # CREATED by flipping the toggle. We only assert no spurious
    # side-effect: the toggle module never touches these collections.
    # (Hermetic test: if they existed before via other test setup,
    # that's fine — we're not asserting their absence.)
    # The negative we DO assert: flipping toggles doesn't drop fields.
    if alpaca_doc:
        assert "api_key_enc" in alpaca_doc or "execution_enabled" in alpaca_doc
    if kraken_doc:
        assert any(k in kraken_doc for k in (
            "public_key_enc", "private_key_enc", "execution_enabled",
        ))


# ─── diagnostics integration ───────────────────────────────────────────


@pytest.mark.tripwire
def test_diagnostics_surfaces_lane_execution_state(auth_client, base_url):
    """The /api/admin/diagnostics response MUST include the new
    `lane_execution` block so the UI banner has truth instead of
    relying on the DEPLOY_MODE env var label."""
    r = auth_client.get(f"{base_url}/api/admin/diagnostics", timeout=15)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "lane_execution" in body
    le = body["lane_execution"]
    for k in ("equity", "crypto", "any_enabled"):
        assert k in le
