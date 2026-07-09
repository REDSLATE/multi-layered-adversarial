"""Shadow-mode tests for `trader.broker` (2026-07-09 iter-22).

Operator directive:

    "There is only one broker door. MC owns it.
     Sidecar cannot submit orders."

The trader sidecar's Kraken + Webull adapters must SHORT-CIRCUIT
before the network call when `TRADER_ENABLED` is falsy — that's
the "one broker door" doctrine. This suite locks in that behavior
so future refactors can't quietly re-enable the sidecar's submit
path.
"""
from __future__ import annotations

import asyncio
import sys

import pytest

sys.path.insert(0, "/app")

from trader import broker  # noqa: E402


# ─── shadow mode (production default) ─────────────────────────────

@pytest.mark.asyncio
async def test_kraken_market_order_short_circuits_when_disabled(monkeypatch):
    """With TRADER_ENABLED absent (production default), Kraken adapter
    returns a `shadow_only=True` receipt and NEVER hits the network."""
    monkeypatch.delenv("TRADER_ENABLED", raising=False)
    # If httpx.AsyncClient is invoked, we've already lost — the
    # fake below detects any network attempt and blows up the test.
    called_out = {"n": 0}

    class _NoNetHttpx:
        async def __aenter__(self):
            called_out["n"] += 1
            raise AssertionError("shadow mode leaked into a network call")

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(broker.httpx, "AsyncClient", _NoNetHttpx)

    result = await broker.kraken_market_order(
        pair="XBTUSD", side="buy", volume="0.001",
    )
    assert isinstance(result, dict)
    assert result["shadow_only"] is True
    assert result["broker"] == "kraken"
    assert result["pair"] == "XBTUSD"
    assert result["side"] == "buy"
    assert result["volume"] == "0.001"
    assert "TRADER_ENABLED=false" in result["reason"]
    assert called_out["n"] == 0


@pytest.mark.asyncio
async def test_webull_market_order_short_circuits_when_disabled(monkeypatch):
    """With TRADER_ENABLED absent, Webull adapter returns a
    `shadow_only=True` receipt and NEVER hits the network."""
    monkeypatch.delenv("TRADER_ENABLED", raising=False)

    class _NoNetHttpx:
        async def __aenter__(self):
            raise AssertionError("shadow mode leaked into a network call")

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(broker.httpx, "AsyncClient", _NoNetHttpx)

    result = await broker.webull_market_order(
        ticker="NVDA", side="BUY",
        notional_usd=100.0, last_price=195.0,
    )
    assert result["shadow_only"] is True
    assert result["broker"] == "webull"
    assert result["ticker"] == "NVDA"
    assert result["side"] == "BUY"
    assert result["notional_usd"] == 100.0
    assert result["last_price"] == 195.0


@pytest.mark.asyncio
@pytest.mark.parametrize("falsy_value", ["false", "0", "no", "off", "", "FALSE"])
async def test_all_falsy_values_are_shadow(monkeypatch, falsy_value):
    """The `TRADER_ENABLED` flag must be tolerant to common falsy
    strings — a typo like `0` or an empty string must NOT bypass
    shadow mode. This locks in fail-CLOSED semantics."""
    monkeypatch.setenv("TRADER_ENABLED", falsy_value)
    result = await broker.kraken_market_order(
        pair="XBTUSD", side="buy", volume="0.001",
    )
    assert result.get("shadow_only") is True, (
        f"TRADER_ENABLED={falsy_value!r} did not shadow — this is "
        "a fail-open bug in _trader_enabled()"
    )


# ─── authoritative mode (opt-in) ───────────────────────────────

@pytest.mark.asyncio
async def test_trader_enabled_true_bypasses_shadow_and_calls_broker(monkeypatch):
    """When TRADER_ENABLED=true, shadow is OFF and the adapter must
    proceed into the credential check + network call. We stop it at
    the credential check to avoid actually hitting Kraken — this
    confirms the shadow gate is NOT the layer stopping the call."""
    monkeypatch.setenv("TRADER_ENABLED", "true")
    # Force the credential check to fail so we know the network
    # would have been reached (but wasn't, safely).
    monkeypatch.delenv("KRAKEN_API_KEY", raising=False)
    monkeypatch.delenv("KRAKEN_API_SECRET", raising=False)

    with pytest.raises(broker.BrokerError) as ei:
        await broker.kraken_market_order(
            pair="XBTUSD", side="buy", volume="0.001",
        )
    # If shadow had bypassed the whole function, we'd get back a
    # shadow_only dict — not a BrokerError. The specific error
    # message confirms we advanced past the shadow gate.
    assert "credentials missing" in str(ei.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("truthy_value", ["true", "1", "yes", "on", "TRUE", "True"])
async def test_all_truthy_values_bypass_shadow(monkeypatch, truthy_value):
    """The `TRADER_ENABLED` flag must recognize all standard truthy
    strings so the operator can flip authority back on without a
    string-format gotcha."""
    monkeypatch.setenv("TRADER_ENABLED", truthy_value)
    monkeypatch.delenv("KRAKEN_API_KEY", raising=False)
    with pytest.raises(broker.BrokerError):
        await broker.kraken_market_order(
            pair="XBTUSD", side="buy", volume="0.001",
        )


# ─── receipts stay useful in shadow ────────────────────────────

@pytest.mark.asyncio
async def test_shadow_receipt_carries_all_intent_fields(monkeypatch):
    """The synthetic shadow receipt must carry every field of the
    intended order so `receipts.jsonl` remains diagnostically
    useful. Post-mortems still need to see WHAT would have fired."""
    monkeypatch.delenv("TRADER_ENABLED", raising=False)

    r_k = await broker.kraken_market_order(
        pair="ADAUSD", side="sell", volume="42.0",
    )
    for key in ("shadow_only", "broker", "reason", "shadowed_at",
                "pair", "side", "volume"):
        assert key in r_k, f"shadow receipt missing {key}"

    r_w = await broker.webull_market_order(
        ticker="SPY", side="SELL",
        notional_usd=50.0, last_price=745.0,
    )
    for key in ("shadow_only", "broker", "reason", "shadowed_at",
                "ticker", "side", "notional_usd", "last_price"):
        assert key in r_w, f"shadow receipt missing {key}"
