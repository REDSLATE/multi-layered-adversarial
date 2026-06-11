"""Tests for the broker_router's Webull override + cap-gate path.

Doctrine pin (operator, 2026-06-10): Webull is a parallel route, not a
replacement. Setting `intent.broker_override = "webull"` redirects a
single intent through Webull while leaving Kraken/Public unchanged.
The router runs the Webull cap evaluator BEFORE adapter load so a
refused order doesn't even probe Webull credentials.
"""
import sys

sys.path.insert(0, "/app/backend")

import pytest

from shared import broker_router
from shared.broker.webull import reset_webull_adapter_for_tests
from shared.broker_router import (
    ADAPTER_LOADERS,
    ROUTE_OVERRIDE_BROKERS,
    BrokerRouteBlocked,
    route_order,
)


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    for key in (
        "WEBULL_ARMED",
        "WEBULL_MIN_NOTIONAL_USD",
        "WEBULL_MAX_NOTIONAL_USD",
        "RISEDUAL_BROKER_REQUIRE_MC_RECEIPT",
        # Force keys empty so the override-routing tests can't reach
        # the live Webull API even when `.env` carries real
        # credentials. Routing must be verifiable by the gate chain
        # alone — adapter behavior is covered by test_webull_adapter.py.
        "WEBULL_APP_KEY",
        "WEBULL_APP_SECRET",
    ):
        monkeypatch.delenv(key, raising=False)
    # Force MC receipt enforcement OFF for these unit tests — we're
    # exercising the lane/override gate, not the MC seal which has
    # its own dedicated coverage.
    monkeypatch.setenv("RISEDUAL_BROKER_REQUIRE_MC_RECEIPT", "false")
    # Reset the process-wide Webull adapter singleton so a prior test
    # (that ran with real creds in env) can't leak a live adapter
    # into these env-emptied tests.
    reset_webull_adapter_for_tests()
    yield
    reset_webull_adapter_for_tests()


def test_webull_is_registered_in_loaders():
    assert "webull" in ADAPTER_LOADERS


def test_webull_is_an_override_broker():
    """Webull MUST be in the override set; Public/Kraken/Alpaca must NOT
    be (the override exists to opt INTO an alternative, not to redirect
    to the lane defaults arbitrarily)."""
    assert "webull" in ROUTE_OVERRIDE_BROKERS
    assert "public" not in ROUTE_OVERRIDE_BROKERS
    assert "kraken" not in ROUTE_OVERRIDE_BROKERS
    assert "alpaca_paper" not in ROUTE_OVERRIDE_BROKERS


@pytest.mark.asyncio
async def test_webull_override_blocked_by_cap_when_disarmed():
    """An intent with broker_override='webull' must be refused at the
    cap gate when WEBULL_ARMED is not set. The router never even
    reaches the adapter load step."""
    intent = {
        "intent_id": "test-1",
        "stack": "alpha",
        "symbol": "AAPL",
        "lane": "equity",
        "action": "BUY",
        "confidence": 0.7,
        "broker_override": "webull",
    }
    with pytest.raises(BrokerRouteBlocked) as exc:
        await route_order(intent, notional_usd=5.0)
    assert "WEBULL_NOT_ARMED" in str(exc.value)


@pytest.mark.asyncio
async def test_webull_override_blocked_above_cap(monkeypatch):
    monkeypatch.setenv("WEBULL_ARMED", "true")
    intent = {
        "intent_id": "test-2",
        "stack": "alpha",
        "symbol": "AAPL",
        "lane": "equity",
        "action": "BUY",
        "confidence": 0.7,
        "broker_override": "webull",
    }
    with pytest.raises(BrokerRouteBlocked) as exc:
        await route_order(intent, notional_usd=25.0)
    assert "ABOVE_CAP" in str(exc.value)


@pytest.mark.asyncio
async def test_webull_override_blocked_below_floor(monkeypatch):
    monkeypatch.setenv("WEBULL_ARMED", "true")
    intent = {
        "intent_id": "test-3",
        "stack": "alpha",
        "symbol": "AAPL",
        "lane": "equity",
        "action": "BUY",
        "confidence": 0.7,
        "broker_override": "webull",
    }
    with pytest.raises(BrokerRouteBlocked) as exc:
        await route_order(intent, notional_usd=1.0)
    assert "BELOW_FLOOR" in str(exc.value)


@pytest.mark.asyncio
async def test_unknown_override_falls_back_to_lane_default(monkeypatch):
    """An intent with `broker_override="public"` (NOT in the override
    set) must NOT redirect — the router silently ignores the value
    and uses the lane default. Doctrine: only opt-IN brokers can be
    selected via override.

    2026-02-19: lane default for equity is now Webull (Public/Alpaca
    deprecated). With WEBULL_ARMED=true and notional above the cap
    band, the fallback path raises an ABOVE_CAP block at the Webull
    cap gate — which itself proves the override was IGNORED (Webull
    cap fires) and the lane default was used.
    """
    monkeypatch.setenv("WEBULL_ARMED", "true")
    intent = {
        "intent_id": "test-4",
        "stack": "alpha",
        "symbol": "AAPL",
        "lane": "equity",
        "action": "BUY",
        "confidence": 0.7,
        "broker_override": "public",  # not in override set — ignored
    }
    # Lane default = webull (post-deprecation). $50 > $10 cap → ABOVE_CAP.
    with pytest.raises(BrokerRouteBlocked) as exc:
        await route_order(intent, notional_usd=50.0)
    assert "ABOVE_CAP" in str(exc.value)


@pytest.mark.asyncio
async def test_crypto_intent_can_override_to_webull(monkeypatch):
    """The override works across BOTH lanes. A crypto intent (which
    would normally go to Kraken) must be redirectable to Webull."""
    monkeypatch.setenv("WEBULL_ARMED", "true")
    intent = {
        "intent_id": "test-5",
        "stack": "alpha",
        "symbol": "BTC-USD",
        "lane": "crypto",
        "action": "BUY",
        "confidence": 0.7,
        "broker_override": "webull",
    }
    # Without WEBULL_APP_KEY/SECRET the adapter loader returns None,
    # so we expect a route-block at adapter-load — NOT at lane-routing,
    # which proves the override was honored.
    with pytest.raises(BrokerRouteBlocked) as exc:
        await route_order(intent, notional_usd=5.0)
    msg = str(exc.value).lower()
    assert "webull" in msg or "no adapter" in msg or "not configured" in msg


@pytest.mark.asyncio
async def test_no_override_uses_lane_default(monkeypatch):
    """Without a broker_override, the lane default applies.

    2026-02-19: equity lane default is now Webull (Public/Alpaca
    deprecated). Without `broker_override` set, the Webull cap gate
    is exercised — proves the lane resolved to Webull as expected.
    """
    monkeypatch.setenv("WEBULL_ARMED", "true")
    intent = {
        "intent_id": "test-6",
        "stack": "alpha",
        "symbol": "AAPL",
        "lane": "equity",
        "action": "BUY",
        "confidence": 0.7,
    }
    # $50 > $10 cap → ABOVE_CAP from the Webull cap gate.
    with pytest.raises(BrokerRouteBlocked) as exc:
        await route_order(intent, notional_usd=50.0)
    assert "ABOVE_CAP" in str(exc.value)
