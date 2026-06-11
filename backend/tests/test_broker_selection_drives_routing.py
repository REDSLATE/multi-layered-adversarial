"""Regression: the broker_selection singleton MUST actually drive routing.

Operator incident (2026-02-19): the UI broker hamburger saved its
selection to the `broker_selection` Mongo singleton, but the brain
never read it and `route_order` always fell through to the lane
default — "flipping the switch to Webull doesn't light up anything on
production". This test pins the fix: when no per-intent
`broker_override` is set, `route_order` consults
`broker_selection.get_current_selection()` and routes accordingly.

Companion pin: `adapter_for_lane` (used by the `broker_connected`
gate in `shared/execution.py`) mirrors the same resolution order so
the pre-trade gate sees the same broker as the live route.
"""
import sys

sys.path.insert(0, "/app/backend")

import pytest

from shared import broker_router
from shared.broker_router import (
    BrokerRouteBlocked,
    adapter_for_lane,
    route_order,
)


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    for key in (
        "WEBULL_ARMED",
        "WEBULL_APP_KEY",
        "WEBULL_APP_SECRET",
        "RISEDUAL_BROKER_REQUIRE_MC_RECEIPT",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("RISEDUAL_BROKER_REQUIRE_MC_RECEIPT", "false")
    yield


def _stub_selection(monkeypatch, equity="webull", crypto="kraken"):
    """Stub `routes.broker_selection.get_current_selection` to return
    a fixed selection without touching Mongo.

    The router imports the function lazily (inside `route_order`), so
    we patch the source module directly.
    """
    async def _fake():
        return {"equity": equity, "crypto": crypto}
    import routes.broker_selection as bs
    monkeypatch.setattr(bs, "get_current_selection", _fake)


@pytest.mark.asyncio
async def test_route_order_consults_broker_selection_for_equity(monkeypatch):
    """With broker_selection.equity = webull AND WEBULL_ARMED=true,
    a normal equity intent (no per-intent override) must hit the
    Webull cap gate — proving the selection drove routing."""
    _stub_selection(monkeypatch, equity="webull", crypto="kraken")
    monkeypatch.setenv("WEBULL_ARMED", "true")
    intent = {
        "intent_id": "sel-1",
        "stack": "alpha",
        "symbol": "AAPL",
        "lane": "equity",
        "action": "BUY",
        "confidence": 0.7,
    }
    with pytest.raises(BrokerRouteBlocked) as exc:
        await route_order(intent, notional_usd=50.0)  # > $10 cap
    assert "ABOVE_CAP" in str(exc.value)


@pytest.mark.asyncio
async def test_route_order_consults_broker_selection_for_crypto(monkeypatch):
    """Crypto intent with selection.crypto = webull must route to
    Webull, not Kraken — Webull cap gate fires for the small-pilot
    notional band."""
    _stub_selection(monkeypatch, equity="webull", crypto="webull")
    monkeypatch.setenv("WEBULL_ARMED", "true")
    intent = {
        "intent_id": "sel-2",
        "stack": "alpha",
        "symbol": "BTC-USD",
        "lane": "crypto",
        "action": "BUY",
        "confidence": 0.7,
    }
    with pytest.raises(BrokerRouteBlocked) as exc:
        await route_order(intent, notional_usd=25.0)
    assert "ABOVE_CAP" in str(exc.value)


@pytest.mark.asyncio
async def test_per_intent_override_still_wins(monkeypatch):
    """A per-intent `broker_override` MUST take precedence over the
    operator selection. Selection sets crypto = kraken; the intent
    sets `broker_override = "webull"` — the cap gate fires."""
    _stub_selection(monkeypatch, equity="webull", crypto="kraken")
    monkeypatch.setenv("WEBULL_ARMED", "true")
    intent = {
        "intent_id": "sel-3",
        "stack": "alpha",
        "symbol": "BTC-USD",
        "lane": "crypto",
        "action": "BUY",
        "confidence": 0.7,
        "broker_override": "webull",
    }
    with pytest.raises(BrokerRouteBlocked) as exc:
        await route_order(intent, notional_usd=25.0)
    assert "ABOVE_CAP" in str(exc.value)


@pytest.mark.asyncio
async def test_selection_lookup_failure_falls_back_to_lane_default(monkeypatch):
    """If `get_current_selection` raises, the router must fall back
    to the lane default — broker_selection is a CONVENIENCE, not a
    hard dependency. Failure here must never kill the trade path."""
    monkeypatch.setenv("WEBULL_ARMED", "true")

    async def _raise():
        raise RuntimeError("mongo blip")
    import routes.broker_selection as bs
    monkeypatch.setattr(bs, "get_current_selection", _raise)

    intent = {
        "intent_id": "sel-4",
        "stack": "alpha",
        "symbol": "AAPL",
        "lane": "equity",
        "action": "BUY",
        "confidence": 0.7,
    }
    # Lane default = webull (post-deprecation). $50 → ABOVE_CAP.
    with pytest.raises(BrokerRouteBlocked) as exc:
        await route_order(intent, notional_usd=50.0)
    assert "ABOVE_CAP" in str(exc.value)


@pytest.mark.asyncio
async def test_adapter_for_lane_mirrors_route_order_selection(monkeypatch):
    """The `broker_connected` gate calls `adapter_for_lane` to find
    out which broker is wired up. It MUST follow the same resolution
    order as `route_order`: per-intent override > broker_selection >
    lane default. Without this, the gate could pass on Public.com
    creds while the live route uses Webull (or vice-versa).
    """
    _stub_selection(monkeypatch, equity="webull", crypto="webull")
    # No env creds means the Webull adapter loader returns None — we
    # care that the resolution PICKED Webull (and so returned None
    # because of missing creds), not that it fell through to Kraken
    # or Public's loader.
    result = await adapter_for_lane("crypto")  # no override
    # Kraken adapter would return a real adapter (Kraken stub doesn't
    # require creds for object construction); Webull returns None when
    # WEBULL_APP_KEY is unset. None proves the lookup honored the
    # selection.
    assert result is None
