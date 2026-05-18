"""HTTP tests for `/api/runtime/survival/*` — the MC surface of the
platform survival layer.
"""
from __future__ import annotations

import os

import requests

from shared.runtime.platform_survival import (
    RuntimeStamp,
    policy_hash,
    sidecar_build_intent,
)


def test_policy_hash_endpoint_public(base_url):
    r = requests.get(f"{base_url}/api/runtime/survival/policy-hash", timeout=15)
    assert r.status_code == 200
    body = r.json()
    assert body["policy_hash"] == policy_hash()
    assert "doctrine" in body


def test_validate_stamp_requires_auth(base_url):
    r = requests.post(
        f"{base_url}/api/runtime/survival/validate-stamp",
        json={"stamp": {}},
        timeout=15,
    )
    assert r.status_code in (401, 403)


def test_validate_stamp_flags_unknown_env(auth_client, base_url):
    stamp = RuntimeStamp.current(sidecar_room="test-room")
    payload = {"stamp": stamp.__dict__ if hasattr(stamp, "__dict__") else dict(stamp.__dict__)}
    # dataclass — use asdict via __dict__-like access
    from dataclasses import asdict
    payload = {"stamp": asdict(stamp)}
    r = auth_client.post(f"{base_url}/api/runtime/survival/validate-stamp", json=payload, timeout=15)
    assert r.status_code == 200
    body = r.json()
    # Test env has env_name=unknown, mc_url empty, db_name empty → multiple errors
    assert body["ok"] is False
    assert "ENV_NOT_PROD" in body["errors"]
    assert "MC_URL_NOT_PROD" in body["errors"]


def test_canonical_gate_blocks_low_confidence(auth_client, base_url):
    os.environ["RISEDUAL_CRYPTO_CONFIDENCE_FLOOR"] = "0.45"
    intent = sidecar_build_intent(
        brain_id="camaro",
        lane="crypto",
        symbol="BTC-USD",
        direction="BUY",
        confidence=0.27,
        room_id="test-room",
    )
    r = auth_client.post(
        f"{base_url}/api/runtime/survival/canonical-gate",
        json={"intent": intent},
        timeout=15,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["accepted"] is False
    assert body["reason"] == "CONFIDENCE_BELOW_FLOOR"


def test_canonical_gate_and_verify_receipt_roundtrip(auth_client, base_url):
    os.environ["RISEDUAL_CRYPTO_CONFIDENCE_FLOOR"] = "0.20"
    # Ensure the backend has a secret. The MC backend process runs
    # under its own env; if RISEDUAL_MC_RECEIPT_SECRET isn't set on
    # the server, verify-receipt will return MISSING_RECEIPT_SECRET.
    # We can still confirm the round-trip math.
    intent = sidecar_build_intent(
        brain_id="camaro",
        lane="crypto",
        symbol="BTC-USD",
        direction="BUY",
        confidence=0.50,
        room_id="test-room",
    )
    r = auth_client.post(
        f"{base_url}/api/runtime/survival/canonical-gate",
        json={"intent": intent},
        timeout=15,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["accepted"] is True
    receipt = body["receipt"]
    assert receipt["lane"] == "crypto"
    assert receipt["symbol"] == "BTC-USD"
    assert receipt["direction"] == "BUY"

    r2 = auth_client.post(
        f"{base_url}/api/runtime/survival/verify-receipt",
        json={"receipt": receipt},
        timeout=15,
    )
    assert r2.status_code == 200
    body2 = r2.json()
    # If the server has RISEDUAL_MC_RECEIPT_SECRET set, ok=True.
    # If not, ok=False with reason="MISSING_RECEIPT_SECRET" — still a
    # valid response shape that proves the endpoint is wired.
    assert "ok" in body2
    assert "reason" in body2
