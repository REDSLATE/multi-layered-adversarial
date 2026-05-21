"""HTTP tests for `/api/admin/brain/doctrine-hint`.

Doctrine pin (2026-02-18):
    The hint endpoint is READ-ONLY scaffolding. It MUST NOT block
    execution. It MUST NOT promote HOLD into trade. Brains MAY
    consult it; ignoring it must not break anything.
"""
from __future__ import annotations

import pytest
import requests


@pytest.mark.tripwire
def test_doctrine_hint_requires_auth(base_url):
    r = requests.get(
        f"{base_url}/api/admin/brain/doctrine-hint?symbol=AAPL", timeout=15,
    )
    assert r.status_code in (401, 403)


@pytest.mark.tripwire
def test_doctrine_hint_returns_candidates_for_large_cap(auth_client, base_url):
    r = auth_client.get(
        f"{base_url}/api/admin/brain/doctrine-hint",
        params={"symbol": "AMZN", "lane": "equity",
                "market_cap_band": "mega"},
        timeout=30,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["primary_doctrine"] == "large_cap_equity_v1"
    assert "large_cap_equity_v1" in body["candidate_doctrines"]
    # small_account remains as fallback candidate.
    assert "small_account_sidecar_v1" in body["candidate_doctrines"]


@pytest.mark.tripwire
def test_doctrine_hint_returns_small_account_when_no_band(auth_client, base_url):
    r = auth_client.get(
        f"{base_url}/api/admin/brain/doctrine-hint",
        params={"symbol": "TEST", "lane": "equity"},
        timeout=30,
    )
    body = r.json()
    assert body["primary_doctrine"] == "small_account_sidecar_v1"


@pytest.mark.tripwire
def test_doctrine_hint_returns_crypto_doctrine_for_crypto_lane(auth_client, base_url):
    r = auth_client.get(
        f"{base_url}/api/admin/brain/doctrine-hint",
        params={"symbol": "BTC/USD", "lane": "crypto"},
        timeout=30,
    )
    body = r.json()
    assert body["primary_doctrine"] == "crypto_sidecar_v1"


@pytest.mark.tripwire
def test_doctrine_hint_shape_contains_emit_semantic(auth_client, base_url):
    """The hint contract is brittle by design — brains rely on these
    field names. Lock the shape."""
    r = auth_client.get(
        f"{base_url}/api/admin/brain/doctrine-hint",
        params={"symbol": "NVDA", "lane": "equity", "strategy": "large_cap"},
        timeout=30,
    )
    body = r.json()
    required = {
        "symbol", "lane", "candidate_doctrines",
        "primary_doctrine", "primary_verdict",
        "recommended_emit_semantic", "state_by_doctrine",
        "doctrine_note",
    }
    assert required <= set(body)
    assert body["recommended_emit_semantic"] in {
        "emit_with_downsize", "emit_normal", "informational_only",
    }


@pytest.mark.tripwire
def test_doctrine_hint_state_per_doctrine_has_verdict(auth_client, base_url):
    r = auth_client.get(
        f"{base_url}/api/admin/brain/doctrine-hint",
        params={"symbol": "AAPL", "lane": "equity",
                "market_cap_band": "large"},
        timeout=30,
    )
    body = r.json()
    for dv, state in body["state_by_doctrine"].items():
        assert "samples" in state
        assert "verdict" in state
        assert state["verdict"] in {
            "LEARNING", "WATCHING",
            "CANDIDATE_PROMOTION", "CANDIDATE_RETIREMENT",
        }


@pytest.mark.tripwire
def test_doctrine_hint_doctrine_note_pins_invariants(auth_client, base_url):
    """The response must explicitly remind callers of the doctrine
    invariants — HOLD never becomes trade, LEARNING never blocks."""
    r = auth_client.get(
        f"{base_url}/api/admin/brain/doctrine-hint",
        params={"symbol": "AAPL", "lane": "equity"},
        timeout=30,
    )
    note = r.json()["doctrine_note"]
    assert "HOLD" in note
    assert "LEARNING" in note
