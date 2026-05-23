"""Tests for the MC-receipt seal inside `shared.broker_router.route_order`.

Covers:
  * Receipt is minted on every order, in both rollout (`enforcement=off`)
    and enforce mode (`RISEDUAL_BROKER_REQUIRE_MC_RECEIPT=true`).
  * Rollout mode: a tampered/missing receipt is LOGGED but the order
    still goes through (so PROD Alpha keeps trading until adoption).
  * Enforce mode: route_order raises `BrokerRouteBlocked` when the
    receipt verify fails.
  * Order metadata carries `mc_receipt_status` + `mc_receipt_enforced`
    so execution receipts can be sliced by signature presence.

We monkeypatch `ADAPTER_LOADERS` with a fake adapter so no real
Alpaca/Kraken keys are required.
"""
from __future__ import annotations

import os

import pytest

from shared import broker_router
from shared.broker_router import (
    BrokerRouteBlocked,
    _broker_require_mc_receipt,
    _mint_and_verify_mc_receipt,
    route_order,
)
from shared.broker_symbol_resolver import AssetKey
from shared.runtime.platform_survival import policy_hash


# Tripwire — this module pins the MC-receipt seal contract.
pytestmark = pytest.mark.tripwire


class _FakeAdapter:
    def __init__(self):
        self.calls = []

    async def submit_market_order(self, *, symbol, notional, side, client_order_id, mc_receipt=None):
        # Adapter under test signature now includes `mc_receipt` (2026-05-23
        # bypass-block doctrine). We accept but don't validate here — the
        # router has already minted it before calling.
        self.calls.append((symbol, notional, side, client_order_id, mc_receipt))
        return {
            "order_id": "fake-1",
            "status": "filled",
            "filled_qty": 1.0,
            "filled_avg_price": 100.0,
            "client_order_id": client_order_id,
            "submitted_at": "2026-05-18T00:00:00+00:00",
            "filled_at": "2026-05-18T00:00:01+00:00",
        }


@pytest.fixture
def fake_alpaca_adapter(monkeypatch):
    fake = _FakeAdapter()

    async def loader():
        return fake

    monkeypatch.setitem(broker_router.ADAPTER_LOADERS, "alpaca_paper", loader)
    return fake


@pytest.fixture
def receipt_secret(monkeypatch):
    monkeypatch.setenv("RISEDUAL_MC_RECEIPT_SECRET", "unit-test-secret")
    monkeypatch.setenv("RISEDUAL_EQUITY_CONFIDENCE_FLOOR", "0.20")
    monkeypatch.setenv("RISEDUAL_CRYPTO_CONFIDENCE_FLOOR", "0.20")


@pytest.fixture
async def broker_thawed():
    """The 2026-05-23 audit phase wrote a freeze row to Mongo. These
    router tests need the broker UNFROZEN — without a fresh thaw, every
    route_order call short-circuits with BrokerRouteBlocked('FROZEN…')."""
    from shared.broker_freeze import thaw
    await thaw(actor="pytest_router_tests", reason="router_test_setup")
    yield


# ─────────────────── enforcement flag ─────────────────────────────────


def test_enforcement_flag_default_on(monkeypatch):
    """Doctrine pin (2026-05-23): after the orphan audit the flag DEFAULTS
    to ON. Bypass is the bug we closed. Operators must explicitly opt OUT."""
    monkeypatch.delenv("RISEDUAL_BROKER_REQUIRE_MC_RECEIPT", raising=False)
    assert _broker_require_mc_receipt() is True


def test_enforcement_flag_explicit_false(monkeypatch):
    """Operators can still opt out by setting the env var explicitly false."""
    for v in ("false", "False", "0", "no", "off"):
        monkeypatch.setenv("RISEDUAL_BROKER_REQUIRE_MC_RECEIPT", v)
        assert _broker_require_mc_receipt() is False


def test_enforcement_flag_truthy_variants(monkeypatch):
    for v in ("true", "True", "1", "yes", "on"):
        monkeypatch.setenv("RISEDUAL_BROKER_REQUIRE_MC_RECEIPT", v)
        assert _broker_require_mc_receipt() is True


# ─────────────────── mint helper ──────────────────────────────────────


def test_mint_helper_synthesizes_stamp_when_intent_lacks_runtime(receipt_secret):
    """An intent posted by a sidecar that hasn't yet adopted the kit has
    no `runtime` field. MC must still mint a valid receipt — the stamp
    is synthesized from MC's own env."""
    intent = {
        "intent_id": "x",
        "stack": "alpha",
        "action": "BUY",
        "confidence": 0.75,
        "symbol": "MSFT",
    }
    asset = AssetKey(canonical="EQ:MSFT", lane="equity", base="MSFT", quote=None)
    check = _mint_and_verify_mc_receipt(
        intent=intent,
        asset=asset,
        side="BUY",
        notional_usd=100.0,
    )
    assert check["ok"] is True
    assert check["reason"] == "VALID_MC_RECEIPT"
    assert check["receipt"]["mc_policy_hash"] == policy_hash()
    assert check["receipt"]["lane"] == "equity"
    assert check["receipt"]["symbol"] == "EQ:MSFT"


def test_mint_helper_uses_sidecar_runtime_when_present(receipt_secret):
    """If the sidecar already adopted the survival kit, its runtime
    stamp comes through on the intent. The mint helper passes it
    through unchanged."""
    sidecar_stamp = {
        "app_name": "alpha",
        "env_name": "prod",
        "git_sha": "abc123",
        "platform": "railway",
        "mc_url": "https://mission.risedual.ai",
        "db_name": "alpha_prod",
        "broker_mode": "live",
        "sidecar_room": "alpha_room",
        "sidecar_version": "1.0.0",
        "policy_hash": policy_hash(),
        "local_execution_authority": False,
        "timestamp_ms": 1700000000000,
    }
    intent = {
        "intent_id": "y",
        "stack": "alpha",
        "action": "BUY",
        "confidence": 0.75,
        "symbol": "MSFT",
        "runtime": sidecar_stamp,
    }
    asset = AssetKey(canonical="EQ:MSFT", lane="equity", base="MSFT", quote=None)
    check = _mint_and_verify_mc_receipt(
        intent=intent,
        asset=asset,
        side="BUY",
        notional_usd=100.0,
    )
    assert check["ok"] is True
    assert check["reason"] == "VALID_MC_RECEIPT"


def test_mint_helper_rejects_sidecar_with_local_authority(receipt_secret):
    """A sidecar that lies about holding local execution authority must
    fail the canonical gate."""
    intent = {
        "intent_id": "z",
        "stack": "alpha",
        "action": "BUY",
        "confidence": 0.75,
        "symbol": "MSFT",
        "runtime": {
            "app_name": "alpha", "env_name": "prod", "git_sha": "abc",
            "platform": "railway", "mc_url": "https://mission.risedual.ai",
            "db_name": "x", "broker_mode": "live", "sidecar_room": "alpha_room",
            "sidecar_version": "1.0", "policy_hash": policy_hash(),
            "local_execution_authority": True,  # ← lie
            "timestamp_ms": 0,
        },
    }
    asset = AssetKey(canonical="EQ:MSFT", lane="equity", base="MSFT", quote=None)
    check = _mint_and_verify_mc_receipt(
        intent=intent,
        asset=asset,
        side="BUY",
        notional_usd=100.0,
    )
    assert check["ok"] is False
    assert "SIDECAR_LOCAL_AUTHORITY_FORBIDDEN" in (check["receipt"].get("reason") or "")


# ─────────────────── route_order integration ─────────────────────────


@pytest.mark.asyncio
async def test_route_order_attaches_receipt_metadata_rollout_mode(
    fake_alpaca_adapter, receipt_secret, broker_thawed, monkeypatch,
):
    # Explicit rollout mode (opt out of the new default-on enforcement).
    monkeypatch.setenv("RISEDUAL_BROKER_REQUIRE_MC_RECEIPT", "false")
    intent = {
        "intent_id": "i1",
        "stack": "alpha",
        "action": "BUY",
        "confidence": 0.75,
        "symbol": "MSFT",
        "lane": "equity",
    }
    order = await route_order(intent, notional_usd=100.0)
    assert order["broker_order_id" if "broker_order_id" in order else "order_id"] == "fake-1"
    assert order["mc_receipt"] is not None
    assert order["mc_receipt_status"] == "VALID_MC_RECEIPT"
    assert order["mc_receipt_enforced"] is False
    assert order["broker"] == "alpaca_paper"
    assert order["lane"] == "equity"
    assert order["canonical"] == "EQ:MSFT"


@pytest.mark.asyncio
async def test_route_order_enforces_when_flag_on_and_receipt_invalid(
    fake_alpaca_adapter, broker_thawed, monkeypatch,
):
    monkeypatch.setenv("RISEDUAL_BROKER_REQUIRE_MC_RECEIPT", "true")
    monkeypatch.delenv("RISEDUAL_MC_RECEIPT_SECRET", raising=False)
    # No secret on server → verify_receipt returns MISSING_RECEIPT_SECRET.
    # With enforcement ON, route_order MUST raise BrokerRouteBlocked.
    intent = {
        "intent_id": "i2",
        "stack": "alpha",
        "action": "BUY",
        "confidence": 0.75,
        "symbol": "MSFT",
        "lane": "equity",
    }
    with pytest.raises(BrokerRouteBlocked) as excinfo:
        await route_order(intent, notional_usd=100.0)
    assert "MC receipt rejected" in str(excinfo.value)
    assert "MISSING_RECEIPT_SECRET" in str(excinfo.value)
    # The fake adapter was NOT called.
    assert fake_alpaca_adapter.calls == []


@pytest.mark.asyncio
async def test_route_order_enforces_and_passes_with_valid_receipt(
    fake_alpaca_adapter, receipt_secret, broker_thawed, monkeypatch,
):
    monkeypatch.setenv("RISEDUAL_BROKER_REQUIRE_MC_RECEIPT", "true")
    intent = {
        "intent_id": "i3",
        "stack": "alpha",
        "action": "BUY",
        "confidence": 0.75,
        "symbol": "MSFT",
        "lane": "equity",
    }
    order = await route_order(intent, notional_usd=100.0)
    assert order["mc_receipt_status"] == "VALID_MC_RECEIPT"
    assert order["mc_receipt_enforced"] is True
    # The fake adapter WAS called.
    assert len(fake_alpaca_adapter.calls) == 1
    assert fake_alpaca_adapter.calls[0][2] == "BUY"


@pytest.mark.asyncio
async def test_route_order_blocks_lying_sidecar_under_enforcement(
    fake_alpaca_adapter, receipt_secret, broker_thawed, monkeypatch,
):
    """A sidecar that claims `local_execution_authority=True` must NOT
    be able to fire a fill under enforcement."""
    monkeypatch.setenv("RISEDUAL_BROKER_REQUIRE_MC_RECEIPT", "true")
    intent = {
        "intent_id": "lie",
        "stack": "alpha",
        "action": "BUY",
        "confidence": 0.75,
        "symbol": "MSFT",
        "lane": "equity",
        "runtime": {
            "app_name": "alpha", "env_name": "prod", "git_sha": "abc",
            "platform": "railway", "mc_url": "https://mission.risedual.ai",
            "db_name": "x", "broker_mode": "live", "sidecar_room": "alpha_room",
            "sidecar_version": "1.0", "policy_hash": policy_hash(),
            "local_execution_authority": True,
            "timestamp_ms": 0,
        },
    }
    with pytest.raises(BrokerRouteBlocked):
        await route_order(intent, notional_usd=100.0)
    assert fake_alpaca_adapter.calls == []
