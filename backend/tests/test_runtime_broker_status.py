"""Tripwires — runtime broker-status endpoint (2026-05-24).

Pins the doctrine:

  1. Brains can READ broker state without holding the keys.
  2. Brains CANNOT read the keys themselves — only previews + state.
  3. Auth: any valid X-Runtime-Token unlocks the endpoint.
  4. No token / wrong token → 401.
  5. Unknown lane → 400.
  6. Response cached server-side for 10s (TTL field exposed in payload).
  7. Both lanes returned in the unified endpoint.
"""
from __future__ import annotations

import os
import pytest

from db import db
from namespaces import KRAKEN_CREDENTIALS
from routes import runtime_broker_status as mod
from routes.runtime_broker_status import (
    _crypto_status, _equity_status, _get_lane_cached,
    broker_status_all, broker_status_lane,
)


pytestmark = [pytest.mark.tripwire]


# ─── helpers ────────────────────────────────────────────────────────


def _reset_cache():
    mod._cache.clear()


@pytest.fixture(autouse=True)
async def _setup(monkeypatch):
    # Real brain ingest token — must match a participant
    monkeypatch.setenv("CAMARO_INGEST_TOKEN", "tw-camaro-token")
    monkeypatch.setenv("REDEYE_INGEST_TOKEN", "tw-redeye-token")
    _reset_cache()
    yield
    _reset_cache()


# ─── auth ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unified_endpoint_requires_token():
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        await broker_status_all(x_runtime_token=None)
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_unified_endpoint_rejects_bogus_token():
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        await broker_status_all(x_runtime_token="not-a-real-token")
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_per_lane_endpoint_requires_token():
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        await broker_status_lane(lane="crypto", x_runtime_token=None)
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_per_lane_endpoint_rejects_bad_lane():
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        await broker_status_lane(lane="ftx", x_runtime_token="tw-camaro-token")
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_unified_endpoint_returns_both_lanes():
    result = await broker_status_all(x_runtime_token="tw-camaro-token")
    assert "crypto" in result
    assert "equity" in result
    assert result["asked_by"] == "camaro"
    assert result["cache_ttl_seconds"] == 10.0


@pytest.mark.asyncio
async def test_any_brain_can_read():
    """Token validation matches against ANY brain's env token."""
    for token, expected_brain in (
        ("tw-camaro-token", "camaro"),
        ("tw-redeye-token", "redeye"),
    ):
        result = await broker_status_all(x_runtime_token=token)
        assert result["asked_by"] == expected_brain


# ─── doctrine: never expose secrets ──────────────────────────────────


@pytest.mark.asyncio
async def test_response_never_includes_full_keys():
    """The doctrinal constraint: status NEVER contains the actual
    public_key or encrypted_private_key. Only previews."""
    # Insert a fake credential doc with the secret fields populated.
    await db[KRAKEN_CREDENTIALS].update_one(
        {"_id": "singleton"},
        {"$set": {
            "public_key": "FAKE_FULL_PUBLIC_KEY_DO_NOT_LEAK_aaaaaaaaaaaaaaaaaa",
            "public_key_preview": "FAKE…aaaa",
            "encrypted_private_key": "FAKE_ENCRYPTED_PRIVATE_KEY_DO_NOT_LEAK_xxxxxxxx",
            "private_key_preview": None,
            "execution_enabled": True,
            "scopes": {"query_funds": True, "trade": False},
            "balance_preview": {"BTC": "0.001"},
        }},
        upsert=True,
    )
    try:
        result = await broker_status_all(x_runtime_token="tw-camaro-token")
        crypto = result["crypto"]
        # Hard assertions: full secret fields must not be in the response
        # AT ANY DEPTH.
        flat = repr(crypto)
        assert "FAKE_FULL_PUBLIC_KEY" not in flat
        assert "FAKE_ENCRYPTED_PRIVATE_KEY" not in flat
        # Preview IS allowed (it's already redacted at ingress).
        assert crypto["public_key_preview"] == "FAKE…aaaa"
        # Scopes ARE allowed — they're not secrets, just permissions.
        assert crypto["scopes"]["query_funds"] is True
        # Balance preview IS allowed (top-3 assets, already redacted).
        assert crypto["balance_preview"] == {"BTC": "0.001"}
    finally:
        await db[KRAKEN_CREDENTIALS].delete_one({"_id": "singleton"})


# ─── shape ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_crypto_shape_when_disconnected():
    # No KRAKEN_CREDENTIALS doc → disconnected shape.
    await db[KRAKEN_CREDENTIALS].delete_one({"_id": "singleton"})
    _reset_cache()
    status = await _crypto_status()
    assert status["lane"] == "crypto"
    assert status["connected"] is False
    assert status["execution_enabled"] is False
    assert status["scopes"] == {}
    assert status["balance_preview"] is None
    assert "lane_execution_enabled" in status
    assert "broker_live_order_enabled" in status
    # last_fill_at / last_error keys present even when disconnected
    assert "last_fill_at" in status
    assert "last_error" in status


@pytest.mark.asyncio
async def test_equity_shape_when_disconnected(monkeypatch):
    # No equity adapter loaded → disconnected shape.
    async def _no_adapter(_lane):
        return None
    monkeypatch.setattr(
        "shared.broker_router.adapter_for_lane", _no_adapter, raising=False,
    )
    _reset_cache()
    status = await _equity_status()
    assert status["lane"] == "equity"
    assert status["connected"] is False
    assert status["execution_enabled"] is False
    assert status["account_state"] is None
    assert "lane_execution_enabled" in status
    assert "broker_live_order_enabled" in status


@pytest.mark.asyncio
async def test_equity_status_when_connected(monkeypatch):
    """2026-02-19 — post-Alpaca-deprecation the equity tile keys off
    `adapter_for_lane("equity")` (Webull). The Alpaca-era
    `account_state` block is no longer surfaced because Webull
    credentials live in env vars rather than a Mongo singleton; the
    brain sidecars size against `last_fill_at` + the explicit
    `connected` boolean."""
    class _FakeAdapter:
        name = "webull"

    async def _live_adapter(_lane):
        return _FakeAdapter()

    monkeypatch.setattr(
        "shared.broker_router.adapter_for_lane", _live_adapter, raising=False,
    )
    _reset_cache()
    status = await _equity_status()
    assert status["lane"] == "equity"
    assert status["connected"] is True
    assert status["execution_enabled"] is True
    assert status["account_state"] is None
    assert "lane_execution_enabled" in status
    assert "broker_live_order_enabled" in status


# ─── cache behavior ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cache_returns_same_object_within_ttl():
    """Two reads inside the 10s TTL window return the SAME cached payload
    (object identity check — confirms we're not re-probing Mongo)."""
    _reset_cache()
    first = await _get_lane_cached("crypto")
    second = await _get_lane_cached("crypto")
    assert first is second


@pytest.mark.asyncio
async def test_cache_separates_lanes():
    """Crypto and equity cache slots are independent."""
    _reset_cache()
    await _get_lane_cached("crypto")
    await _get_lane_cached("equity")
    assert "crypto" in mod._cache
    assert "equity" in mod._cache
    assert mod._cache["crypto"][1]["lane"] == "crypto"
    assert mod._cache["equity"][1]["lane"] == "equity"
