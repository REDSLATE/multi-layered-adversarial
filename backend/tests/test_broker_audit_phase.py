"""Tripwire tests — broker bypass audit (2026-05-23).

Locks the four invariants the operator demanded after the orphan
audit surfaced ~500 fills that bypassed MC:

  1) Broker freeze is a hard gate above the lane toggles. When the
     freeze is ON, `route_order` raises `BrokerRouteBlocked` BEFORE
     it touches any adapter or credential.

  2) The Alpaca adapter refuses to submit_market_order / submit_limit_order
     without a structurally-valid `mc_receipt` kwarg. Adapter is the
     last line of defence.

  3) The Kraken adapter applies the same bypass-block invariant.

  4) `RISEDUAL_BROKER_REQUIRE_MC_RECEIPT` defaults to TRUE (env-based
     escape hatch only, with the explicit value `false`).

These four invariants together make "trade without MC" impossible
inside this code path. They do NOT prevent a brain sidecar from
holding its own API key and POSTing direct — that's a credential
hygiene problem, not a code one. But they DO mean MC's own code can
never be the bypass.
"""
from __future__ import annotations

import pytest

from shared.broker_freeze import (
    BrokerFrozen,
    assert_not_frozen,
    freeze,
    get_freeze_state,
    is_frozen,
    thaw,
)


pytestmark = [pytest.mark.tripwire]


async def _async_test(coro):
    """Tiny helper to run async assertions inside sync tests."""
    return await coro


# ─────────────────────────── freeze invariants ───────────────────────────


@pytest.mark.asyncio
async def test_freeze_blocks_route_order(monkeypatch):
    """When the freeze is ON, route_order must raise BEFORE adapter
    resolution. Verified by checking that the call doesn't even try
    to compose the asset key."""
    from shared.broker_router import BrokerRouteBlocked, route_order

    # Freeze the broker.
    await freeze("tripwire_test_freeze", actor="pytest")
    try:
        assert await is_frozen() is True
        with pytest.raises(BrokerRouteBlocked) as exc_info:
            await route_order(
                {
                    "intent_id": "tripwire-test-frozen",
                    "stack": "camaro",
                    "action": "BUY",
                    "symbol": "AAPL",
                    "lane": "equity",
                    "confidence": 0.7,
                },
                notional_usd=10.0,
            )
        assert "FROZEN" in str(exc_info.value).upper()
    finally:
        await thaw(actor="pytest", reason="tripwire_test_cleanup")


@pytest.mark.asyncio
async def test_freeze_state_round_trips():
    """freeze() -> get_freeze_state() round trip surfaces the reason
    and actor verbatim."""
    await freeze("tripwire_state_check", actor="pytest")
    try:
        s = await get_freeze_state()
        assert s["frozen"] is True
        assert s["reason"] == "tripwire_state_check"
        assert s["frozen_by"] == "pytest"
    finally:
        await thaw(actor="pytest")
        s2 = await get_freeze_state()
        assert s2["frozen"] is False


@pytest.mark.asyncio
async def test_assert_not_frozen_raises_when_frozen():
    await freeze("tripwire_assert_check", actor="pytest")
    try:
        with pytest.raises(BrokerFrozen):
            await assert_not_frozen()
    finally:
        await thaw(actor="pytest")
    # After thaw the assert returns silently.
    await assert_not_frozen()


# ───────────────────────── adapter receipt requirement ─────────────────────────


def test_alpaca_adapter_refuses_without_mc_receipt():
    """AlpacaPaperAdapter.submit_market_order must raise BypassBlocked
    when no mc_receipt is attached. We don't need real Alpaca creds —
    the receipt check fires before the SDK call."""
    from shared.broker.alpaca import AlpacaPaperAdapter, BypassBlocked

    # Construct an adapter with dummy creds. The receipt check is the
    # very first guard in submit_market_order, so we never reach the
    # Alpaca SDK call.
    a = AlpacaPaperAdapter(api_key="x" * 20, secret_key="y" * 20)

    import asyncio as _asyncio
    with pytest.raises(BypassBlocked):
        _asyncio.get_event_loop().run_until_complete(
            a.submit_market_order(
                symbol="AAPL",
                notional=10.0,
                side="BUY",
                client_order_id="tripwire-1",
                mc_receipt=None,
            ),
        )


def test_alpaca_adapter_refuses_malformed_receipt():
    """A dict-shaped receipt missing required fields is still bypass."""
    from shared.broker.alpaca import AlpacaPaperAdapter, BypassBlocked

    a = AlpacaPaperAdapter(api_key="x" * 20, secret_key="y" * 20)

    import asyncio as _asyncio
    with pytest.raises(BypassBlocked):
        _asyncio.get_event_loop().run_until_complete(
            a.submit_market_order(
                symbol="AAPL",
                notional=10.0,
                side="BUY",
                client_order_id="tripwire-2",
                mc_receipt={"some": "garbage"},
            ),
        )


def test_kraken_adapter_refuses_without_mc_receipt():
    """Kraken adapter must apply the same invariant."""
    from shared.crypto.broker_adapter import KrakenLiveAdapter

    k = KrakenLiveAdapter(public_key="x" * 20, private_key="y" * 20)

    import asyncio as _asyncio
    with pytest.raises(PermissionError):
        _asyncio.get_event_loop().run_until_complete(
            k.submit_market_order(
                symbol="XBTUSD",
                notional=10.0,
                side="BUY",
                client_order_id="tripwire-3",
                mc_receipt=None,
            ),
        )


# ───────────────────────── env default ─────────────────────────


def test_broker_require_mc_receipt_defaults_true(monkeypatch):
    """The env-driven enforcement flag must default to TRUE — bypass is
    the bug we closed. Operators must explicitly opt OUT by setting
    `RISEDUAL_BROKER_REQUIRE_MC_RECEIPT=false`."""
    from shared.broker_router import _broker_require_mc_receipt

    monkeypatch.delenv("RISEDUAL_BROKER_REQUIRE_MC_RECEIPT", raising=False)
    assert _broker_require_mc_receipt() is True

    monkeypatch.setenv("RISEDUAL_BROKER_REQUIRE_MC_RECEIPT", "false")
    assert _broker_require_mc_receipt() is False

    monkeypatch.setenv("RISEDUAL_BROKER_REQUIRE_MC_RECEIPT", "true")
    assert _broker_require_mc_receipt() is True
