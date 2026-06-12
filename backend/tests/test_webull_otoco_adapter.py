"""Atomic OTOCO bracket (Webull v3 combo) — adapter unit tests.

Tests `WebullAdapter.submit_otoco_market` without touching the live
broker. The SDK call (`order_v3.place_order`) is stubbed via mocks so
the test pins:

  * Doctrine sanity of the bracket geometry (stop < entry < target
    for BUY; inverse for SELL).
  * The combo payload shape — 3 legs with the right combo_type
    (MASTER + 2× OTOCO), correct sides (BUY entry → SELL children;
    SELL entry → BUY children), correct order_type per leg
    (MARKET / LIMIT / STOP), `quantity` as a stringified integer,
    `entrust_type=QTY` (NOT AMOUNT — Webull combo doesn't support
    fractional).
  * `client_combo_order_id` carried on the request.
  * Integer-qty requirement (fractional intents must reject).
  * Armed-flag re-check.
  * Returns the right BrokerOrder shape with combo-level extras
    (`combo_order_id`, `tp_client_order_id`, `sl_client_order_id`).
  * Malformed brackets (stop above entry on BUY, etc.) fail
    closed BEFORE any SDK call is made.

Doctrine: this method exists as a parallel capability to the small-
pilot $1-$10 fractional market path. Fractional intents stay on
`submit_market_order`; whole-share intents with a published thesis
can opt into atomic OTOCO.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from dotenv import load_dotenv  # noqa: E402
load_dotenv("/app/backend/.env")

from shared.broker.webull import WebullAdapter  # noqa: E402
from shared.broker.webull_caps import WebullCapBlocked  # noqa: E402


# ── Helpers ───────────────────────────────────────────────────────────


def _make_adapter(last_price: float = 100.0):
    """Build a WebullAdapter with the SDK + caches stubbed so no real
    network call fires. The SDK response mimics Webull's documented
    {code, data} envelope."""
    a = WebullAdapter(api_client=MagicMock())
    # Skip the account-id lookup.
    a.account_id = "test-account-1"
    # Skip the instrument-id resolve (the real path hits the broker).
    a._resolve_instrument_id = AsyncMock(  # type: ignore[method-assign]
        return_value=("INSTRUMENT-1", last_price, True),
    )
    # Capture the SDK call instead of hitting the broker.
    captured = {}

    async def _fake_sdk(fn, *args, **kwargs):
        # Webull's order_v3.place_order receives positional args:
        # (account_id, new_orders, client_combo_order_id)
        captured["args"] = args
        captured["fn_qualname"] = getattr(fn, "__qualname__", str(fn))
        # Return a minimal envelope shaped like a real response.
        rv = MagicMock()
        rv.json.return_value = {
            "code": "200",
            "data": {
                "orderId": "WB-ORDER-MASTER-1",
                "status": "SUBMITTED",
                "createTime": "2026-02-19T10:00:00Z",
            },
        }
        return rv

    a._sdk_call = _fake_sdk  # type: ignore[method-assign]
    # The TradeClient is built lazily; stub it once with an attribute
    # that resolves the `.order_v3.place_order` chain.
    fake_trade = MagicMock()
    fake_trade.order_v3.place_order = MagicMock(name="order_v3.place_order")
    a._trade_client = fake_trade
    return a, captured


@pytest.fixture(autouse=True)
def _armed(monkeypatch):
    """Force armed flag on for tests — the cap evaluator is exercised
    in its own test module."""
    monkeypatch.setenv("WEBULL_ARMED", "true")


# ── Geometry / sanity ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_buy_bracket_geometry_must_be_coherent():
    """BUY with stop ABOVE entry → reject before any SDK call."""
    a, captured = _make_adapter(last_price=100.0)
    with pytest.raises(RuntimeError, match="OTOCO BUY bracket malformed"):
        await a.submit_otoco_market(
            symbol="AAPL", qty=2, side="BUY",
            target_price=110.0,  # OK
            stop_price=105.0,    # WRONG — above entry
        )
    assert "args" not in captured, "SDK must not be hit on malformed bracket"


@pytest.mark.asyncio
async def test_sell_bracket_geometry_must_be_coherent():
    """SELL with stop BELOW entry → reject before any SDK call."""
    a, captured = _make_adapter(last_price=100.0)
    with pytest.raises(RuntimeError, match="OTOCO SELL bracket malformed"):
        await a.submit_otoco_market(
            symbol="AAPL", qty=2, side="SELL",
            target_price=90.0,   # OK (target below entry on a short)
            stop_price=95.0,     # WRONG — below entry
        )


@pytest.mark.asyncio
async def test_fractional_qty_rejected():
    """OTOCO requires integer share qty; fractional → reject."""
    a, _ = _make_adapter()
    with pytest.raises(RuntimeError, match="integer qty"):
        await a.submit_otoco_market(
            symbol="AAPL", qty=0.5, side="BUY",
            target_price=110.0, stop_price=90.0,
        )


@pytest.mark.asyncio
async def test_zero_qty_rejected():
    a, _ = _make_adapter()
    with pytest.raises(RuntimeError, match="integer qty"):
        await a.submit_otoco_market(
            symbol="AAPL", qty=0, side="BUY",
            target_price=110.0, stop_price=90.0,
        )


@pytest.mark.asyncio
async def test_armed_flag_required(monkeypatch):
    a, _ = _make_adapter()
    monkeypatch.setenv("WEBULL_ARMED", "false")
    with pytest.raises(WebullCapBlocked, match="WEBULL_NOT_ARMED"):
        await a.submit_otoco_market(
            symbol="AAPL", qty=1, side="BUY",
            target_price=110.0, stop_price=90.0,
        )


# ── Payload shape ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_buy_otoco_builds_three_legs_with_correct_sides():
    a, captured = _make_adapter(last_price=100.0)
    res = await a.submit_otoco_market(
        symbol="AAPL", qty=3, side="BUY",
        target_price=110.0, stop_price=95.0,
        client_order_id="mc-abc12345",
    )
    # Captured: (account_id, new_orders, combo_id)
    account_id, new_orders, combo_id = captured["args"]
    assert account_id == "test-account-1"
    assert combo_id.startswith("combo-")
    assert isinstance(new_orders, list) and len(new_orders) == 3

    master, tp, sl = new_orders
    # MASTER: market entry on the requested side
    assert master["combo_type"] == "MASTER"
    assert master["order_type"] == "MARKET"
    assert master["side"] == "BUY"
    assert master["entrust_type"] == "QTY"
    assert master["quantity"] == "3"
    assert master["symbol"] == "AAPL"
    assert master["instrument_id"] == "INSTRUMENT-1"

    # TP child: LIMIT sell at target_price (BUY entry → SELL TP)
    assert tp["combo_type"] == "OTOCO"
    assert tp["order_type"] == "LIMIT"
    assert tp["side"] == "SELL"
    assert tp["limit_price"] == "110.00"
    assert tp["quantity"] == "3"
    assert tp["client_order_id"].startswith("tp-")

    # SL child: STOP sell at stop_price (BUY entry → SELL SL)
    assert sl["combo_type"] == "OTOCO"
    assert sl["order_type"] == "STOP"
    assert sl["side"] == "SELL"
    assert sl["stop_price"] == "95.00"
    assert sl["quantity"] == "3"
    assert sl["client_order_id"].startswith("sl-")

    # Returned BrokerOrder envelope carries combo-level identifiers
    # so the resolver / cancel paths can target the OCO pair.
    assert res["combo_order_id"] == "WB-ORDER-MASTER-1"
    assert res["combo_client_order_id"] == combo_id
    assert res["tp_client_order_id"] == tp["client_order_id"]
    assert res["sl_client_order_id"] == sl["client_order_id"]
    assert res["tp_limit_price"] == 110.0
    assert res["sl_stop_price"] == 95.0
    assert res["entry_proxy_price"] == 100.0
    assert res["type"] == "otoco_market"


@pytest.mark.asyncio
async def test_sell_otoco_builds_three_legs_with_correct_sides():
    a, captured = _make_adapter(last_price=100.0)
    res = await a.submit_otoco_market(
        symbol="AAPL", qty=2, side="SELL",
        target_price=92.0,   # short profit-target (below entry)
        stop_price=108.0,    # short stop-loss (above entry)
    )
    _account, new_orders, _combo = captured["args"]
    master, tp, sl = new_orders
    assert master["side"] == "SELL"
    # SELL entry → BUY children (covering)
    assert tp["side"] == "BUY"
    assert sl["side"] == "BUY"
    assert tp["order_type"] == "LIMIT"
    assert sl["order_type"] == "STOP"
    assert tp["limit_price"] == "92.00"
    assert sl["stop_price"] == "108.00"
    # All three quantities match the entry qty.
    assert master["quantity"] == "2"
    assert tp["quantity"] == "2"
    assert sl["quantity"] == "2"
    assert res["side"] == "SELL"


@pytest.mark.asyncio
async def test_sdk_envelope_error_surfaces_runtime_error():
    a, _ = _make_adapter()

    async def _err_sdk(fn, *args, **kwargs):
        rv = MagicMock()
        rv.json.return_value = {
            "code": "40010",
            "msg": "INSUFFICIENT_FUNDS",
        }
        return rv

    a._sdk_call = _err_sdk  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="code=40010"):
        await a.submit_otoco_market(
            symbol="AAPL", qty=1, side="BUY",
            target_price=110.0, stop_price=90.0,
        )


@pytest.mark.asyncio
async def test_negative_prices_rejected():
    a, _ = _make_adapter()
    with pytest.raises(RuntimeError, match="must be positive"):
        await a.submit_otoco_market(
            symbol="AAPL", qty=1, side="BUY",
            target_price=-1.0, stop_price=90.0,
        )
