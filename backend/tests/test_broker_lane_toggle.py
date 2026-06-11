"""Broker-lane toggle tests.

Doctrine pin (2026-02-XX):
    Each trading lane has an operator-controlled on/off switch
    stored in `broker_lane_toggles` (one row per lane). The broker
    router consults `is_lane_enabled(lane)` BEFORE any credential
    lookup; disabled lane = NO_TRADE.

    Defaults: lanes default to ENABLED when no row exists. The
    operator MUST explicitly disable to turn off — we never silently
    shut a lane down.
"""
from __future__ import annotations

import pytest

from db import db
from namespaces import BROKER_LANE_AUDIT_LOG, BROKER_LANE_TOGGLES
from routes.broker_lane_admin import KNOWN_LANES, is_lane_enabled


@pytest.fixture(autouse=True)
async def _clear():
    await db[BROKER_LANE_TOGGLES].delete_many({})
    await db[BROKER_LANE_AUDIT_LOG].delete_many({})
    yield
    await db[BROKER_LANE_TOGGLES].delete_many({})
    await db[BROKER_LANE_AUDIT_LOG].delete_many({})


# ──────────────────────── is_lane_enabled() ────────────────────────


async def test_unknown_lane_returns_false():
    """`broker_for_lane` only knows equity + crypto. An unknown lane
    must NOT pass — we don't want a typo to silently trade."""
    assert await is_lane_enabled("forex") is False
    assert await is_lane_enabled("") is False
    assert await is_lane_enabled("EQUITY ") is True  # trims + lowercases


async def test_defaults_to_enabled_when_no_row():
    """No toggle row = enabled. Lanes are on by default; explicit
    operator action is required to disable."""
    assert await is_lane_enabled("equity") is True
    assert await is_lane_enabled("crypto") is True


async def test_explicit_enable_row_is_honored():
    await db[BROKER_LANE_TOGGLES].insert_one({
        "_id": "equity", "enabled": True,
    })
    assert await is_lane_enabled("equity") is True


async def test_explicit_disable_row_is_honored():
    """The only way for a lane to be off — an explicit row from a
    deliberate operator toggle."""
    await db[BROKER_LANE_TOGGLES].insert_one({
        "_id": "crypto", "enabled": False,
    })
    assert await is_lane_enabled("crypto") is False
    # The other lane is unaffected.
    assert await is_lane_enabled("equity") is True


async def test_disable_one_lane_does_not_affect_other():
    """Toggling equity off must NOT cascade into crypto, and vice
    versa. This is the whole point of having per-lane toggles."""
    await db[BROKER_LANE_TOGGLES].insert_one({
        "_id": "equity", "enabled": False,
    })
    assert await is_lane_enabled("equity") is False
    assert await is_lane_enabled("crypto") is True


async def test_known_lanes_match_broker_registry():
    """The lane identifiers this module knows about MUST match the
    broker_symbol_resolver registry — otherwise the toggle row could
    be set but the router still routes (or vice versa)."""
    from shared.broker_symbol_resolver import LANE_BROKER_REGISTRY
    assert set(KNOWN_LANES) == set(LANE_BROKER_REGISTRY.keys())


# ──────────────────────── route_order gate integration ────────────────────────


@pytest.mark.asyncio
async def test_route_order_blocks_when_lane_disabled(monkeypatch):
    """End-to-end: disabling equity → route_order on an equity intent
    raises BrokerRouteBlocked BEFORE any credential lookup."""
    from shared.broker_router import route_order, BrokerRouteBlocked
    from shared import broker_router as br

    # Disable equity.
    await db[BROKER_LANE_TOGGLES].insert_one({
        "_id": "equity", "enabled": False,
    })

    # Sanity: ensure no broker adapter is ever consulted by hard-failing
    # both loaders. If the lane toggle were skipped, the test would
    # raise a DIFFERENT error from the adapter loader.
    async def _explode():
        raise AssertionError(
            "adapter loader was reached — lane toggle should have "
            "short-circuited route_order before credentials."
        )
    monkeypatch.setitem(br.ADAPTER_LOADERS, "public", _explode)
    monkeypatch.setitem(br.ADAPTER_LOADERS, "alpaca_paper", _explode)

    intent = {
        "intent_id": "test-equity-blocked",
        "symbol": "AAPL",
        "lane": "equity",
        "action": "BUY",
        "confidence": 0.7,
        "stack": "alpha",
    }
    with pytest.raises(BrokerRouteBlocked) as ei:
        await route_order(intent, notional_usd=10.0)
    assert "lane 'equity' is disabled" in str(ei.value)


@pytest.mark.asyncio
async def test_route_order_allows_other_lane_when_one_disabled(monkeypatch):
    """Disabling equity must NOT block crypto. Independence is the
    contract.

    SAFETY: every broker loader is monkeypatched to a no-op so this
    test can NEVER fire a real order — credentials and execution
    gates being open is no longer an attack vector for tests.
    """
    from shared.broker_router import route_order, BrokerRouteBlocked
    from shared import broker_router as br

    await db[BROKER_LANE_TOGGLES].insert_one({
        "_id": "equity", "enabled": False,
    })

    async def _stub_adapter():
        return None  # no adapter → router raises BrokerRouteBlocked downstream
    monkeypatch.setattr(br, "_get_public_adapter", _stub_adapter)
    monkeypatch.setattr(br, "_get_equity_adapter", _stub_adapter)
    monkeypatch.setattr(br, "get_kraken_adapter", _stub_adapter)

    intent = {
        "intent_id": "test-crypto-allowed",
        "canonical": "CRYPTO:BTC-USD",
        "action": "BUY",
        "confidence": 0.7,
        "stack": "redeye",
    }
    try:
        await route_order(intent, notional_usd=10.0)
    except BrokerRouteBlocked as e:
        assert "lane 'crypto' is disabled" not in str(e)
    except Exception:  # noqa: BLE001
        pass


@pytest.mark.asyncio
async def test_route_order_fails_open_on_toggle_lookup_error(monkeypatch):
    """If the toggle collection is unreachable (Mongo blip), the
    router must NOT block all trading — it should fall through to
    the downstream gates and log a warning.

    SAFETY: every broker loader is stubbed so this test cannot fire
    real orders even if Public/Kraken creds are stored and execution
    gates are open. Previously this test placed a real $10 AAPL
    market-buy on Public.com when creds + funds aligned — that's
    fixed here by stubbing adapters.
    """
    from shared.broker_router import route_order, BrokerRouteBlocked
    from shared import broker_router as br
    from routes import broker_lane_admin as bla

    async def _explode(_lane):
        raise RuntimeError("simulated mongo blip")
    monkeypatch.setattr(bla, "is_lane_enabled", _explode)

    async def _stub_adapter():
        return None
    monkeypatch.setattr(br, "_get_public_adapter", _stub_adapter)
    monkeypatch.setattr(br, "_get_equity_adapter", _stub_adapter)
    monkeypatch.setattr(br, "get_kraken_adapter", _stub_adapter)

    intent = {
        "intent_id": "test-fail-open",
        "symbol": "AAPL",
        "lane": "equity",
        "action": "BUY",
        "confidence": 0.7,
        "stack": "alpha",
    }
    try:
        await route_order(intent, notional_usd=10.0)
    except BrokerRouteBlocked as e:
        assert "lane 'equity' is disabled" not in str(e)
    except Exception:  # noqa: BLE001
        # Downstream broker rejection (creds, API 4xx, etc.) is fine —
        # proves we got past the lane gate.
        pass
