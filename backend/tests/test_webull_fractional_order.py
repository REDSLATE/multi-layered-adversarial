"""Tests for the Webull fractional-share path (place_order_v2 + QTY decimal).

Doctrine (2026-02-26, supersedes 2026-02-19 AMOUNT approach):
    Webull's v2 API DEPRECATED the `entrust_type="AMOUNT"` +
    `total_cash_amount` fractional path. Sending that combo now
    triggers HTTP 417 / INVALID_PARAMETER with the misleading
    message "The time you sent is not supported." (Verified live
    against Webull's OpenAPI 2026-02-26.)

    The current fractional path is:
        place_order_v2(account_id, stock_order_dict)
    with `entrust_type="QTY"` + `quantity="<decimal>"` (a string,
    decimal precision supported for US equities) and
    `order_type="LIMIT"` with a slippage-adjusted `limit_price`
    computed from `last_price`.

These tests pin:
  * The v2 SDK method is called (not v1) for notional intents.
  * The stock_order dict carries entrust_type=QTY + decimal quantity
    string + LIMIT order_type + limit_price computed from last_price.
  * The truncated quantity keeps cash spend within the notional cap.
  * SELL intents route through the same fractional path.
  * The qty (whole-share) legacy path still works for callers that
    pass qty explicitly.
"""
from __future__ import annotations

import sys
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, "/app/backend")

from shared.broker.webull import WebullAdapter, reset_webull_adapter_for_tests


class _StubApiClient:
    def add_endpoint(self, *_a, **_kw):
        pass


class _CapturingOrderClient:
    """Stand-in for `trade_client.order`. Records the args of every
    SDK call so the test can assert WHAT was sent to Webull."""

    def __init__(self) -> None:
        self.place_order_v2_calls: list[tuple] = []
        self.place_order_calls: list[tuple] = []

    def place_order_v2(self, account_id, stock_order):
        self.place_order_v2_calls.append((account_id, stock_order))

        class _Res:
            def json(self_inner):
                return {
                    "code": "200",
                    "data": {
                        "orderId": "WB-FRACTIONAL-1",
                        "clientOrderId": stock_order["client_order_id"],
                        "status": "SUBMITTED",
                    },
                }
        return _Res()

    def place_order(self, *args, **kwargs):
        self.place_order_calls.append((args, kwargs))

        class _Res:
            def json(self_inner):
                return {
                    "code": "200",
                    "data": {
                        "orderId": "WB-WHOLE-1",
                        "clientOrderId": args[4] if len(args) >= 5 else None,
                        "status": "SUBMITTED",
                    },
                }
        return _Res()


class _StubTradeClient:
    def __init__(self) -> None:
        self.order = _CapturingOrderClient()
        # account_v2 is unused in these tests — we mock _resolve_account_id
        # directly so get_account_balance is never called.
        self.account_v2 = None


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    for key in (
        "WEBULL_ARMED",
        "WEBULL_MIN_NOTIONAL_USD",
        "WEBULL_MAX_NOTIONAL_USD",
        "WEBULL_LIMIT_SLIPPAGE_BPS",
        "WEBULL_EXTENDED_HOURS_SLIPPAGE_BPS",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("WEBULL_ARMED", "true")
    monkeypatch.setenv("WEBULL_MIN_NOTIONAL_USD", "1.00")
    monkeypatch.setenv("WEBULL_MAX_NOTIONAL_USD", "10.00")
    # Pin slippage to a stable value so quantity math is predictable.
    monkeypatch.setenv("WEBULL_LIMIT_SLIPPAGE_BPS", "50")
    monkeypatch.setenv("WEBULL_EXTENDED_HOURS_SLIPPAGE_BPS", "50")
    reset_webull_adapter_for_tests()
    yield
    reset_webull_adapter_for_tests()


def _adapter_with_instrument(symbol: str, instrument_id: str, last_price: float):
    """Build an adapter pre-wired to bypass the network calls:
       - _resolve_account_id → SUB123
       - _resolve_instrument_id(symbol) → (instrument_id, last_price, True)
       - _trade()                       → capturing stub
    """
    a = WebullAdapter(api_client=_StubApiClient(), account_id="SUB123")
    a._resolve_account_id = AsyncMock(return_value="SUB123")  # type: ignore[method-assign]
    a._resolve_instrument_id = AsyncMock(  # type: ignore[method-assign]
        return_value=(instrument_id, last_price, True)
    )
    a._trade_client = _StubTradeClient()
    return a


# ── core invariant: fractional path is used for notional intents ───


@pytest.mark.asyncio
async def test_notional_buy_uses_place_order_v2_with_qty_decimal():
    """The whole point of the rev: a notional intent must hit v2 +
    entrust_type=QTY (decimal quantity) — NEVER the v1 integer path.
    """
    adapter = _adapter_with_instrument("NVDA", "913355100", 140.0)
    result = await adapter.submit_market_order("NVDA", notional=1.00, side="BUY")

    trade = adapter._trade_client
    assert len(trade.order.place_order_v2_calls) == 1, (
        "exactly one place_order_v2 call expected for fractional notional"
    )
    assert len(trade.order.place_order_calls) == 0, (
        "v1 place_order must NOT be called for notional intents — "
        "the integer-only path is the bug the operator just hit"
    )

    account_id, stock_order = trade.order.place_order_v2_calls[0]
    assert account_id == "SUB123"
    # 2026-02-26 doctrine: QTY + decimal, NOT AMOUNT + total_cash_amount.
    assert stock_order["entrust_type"] == "QTY", (
        "fractional path must set entrust_type=QTY (Webull deprecated AMOUNT)"
    )
    assert "total_cash_amount" not in stock_order, (
        "AMOUNT-mode field must NOT be present — Webull now rejects it"
    )
    # Quantity is a decimal string. For $1 NVDA @ $140 with a 50bps
    # LIMIT slippage band, quantity ≈ 1.00 / (140 * 1.005) ≈ 0.007107.
    assert isinstance(stock_order["quantity"], str), (
        "quantity must be a string per Webull v2 docs"
    )
    assert "." in stock_order["quantity"], (
        "quantity must carry decimal precision for fractional US equities"
    )
    qty_val = float(stock_order["quantity"])
    assert 0 < qty_val < 1.0, (
        f"expected fractional qty <1 share for $1 NVDA @ $140, got {qty_val}"
    )
    # 2026-02-26 doctrine: equity always LIMIT (not MARKET).
    assert stock_order["order_type"] == "LIMIT", (
        "equity fractional path must always use LIMIT (Webull rejects "
        "AMOUNT+MARKET with HTTP 417)"
    )
    assert "limit_price" in stock_order
    assert stock_order["symbol"] == "NVDA"
    assert stock_order["side"] == "BUY"
    assert stock_order["time_in_force"] == "DAY"
    assert stock_order["instrument_type"] == "EQUITY"
    assert stock_order["market"] == "US"
    assert stock_order["support_trading_session"] in {"CORE", "ALL"}

    # The returned order receipt reflects the fractional intent.
    assert result["symbol"] == "NVDA"
    assert result["notional"] == 1.00
    assert result["side"] == "BUY"
    assert result["status"] in {"SUBMITTED", "FILLED", "PENDING"}


@pytest.mark.asyncio
async def test_one_dollar_nvda_no_longer_blocked():
    """The operator's exact case: $1 of NVDA at ~$140. Pre-fix this
    raised WEBULL_QTY_BELOW_ONE. Post-fix it MUST submit cleanly."""
    adapter = _adapter_with_instrument("NVDA", "913355100", 140.0)
    # Should NOT raise.
    result = await adapter.submit_market_order("NVDA", notional=1.00, side="BUY")
    assert result["notional"] == 1.00


@pytest.mark.asyncio
async def test_ten_dollar_aapl_uses_qty_decimal_not_qty_rounding():
    """A $10 intent on AAPL (~$225) is the worst case the old code
    handled — it bailed with QTY_BELOW_ONE. With v2/QTY-decimal mode
    the broker computes the sub-share cash spend from limit_price ×
    quantity, and the truncated 6dp quantity keeps spend ≤ notional.
    """
    adapter = _adapter_with_instrument("AAPL", "913256135", 225.0)
    result = await adapter.submit_market_order("AAPL", notional=10.00, side="BUY")

    trade = adapter._trade_client
    assert len(trade.order.place_order_v2_calls) == 1
    _, stock_order = trade.order.place_order_v2_calls[0]
    assert stock_order["entrust_type"] == "QTY"
    assert stock_order["order_type"] == "LIMIT"
    qty_val = float(stock_order["quantity"])
    limit_price = float(stock_order["limit_price"])
    # Truncated quantity × limit price must stay within the notional cap.
    assert qty_val * limit_price <= 10.00, (
        f"truncated qty {qty_val} × limit {limit_price} must stay ≤ notional 10.00"
    )
    # $10 on AAPL @ ~$225 → definitely fractional (< 1 share).
    assert 0 < qty_val < 1.0
    assert result["notional"] == 10.00


@pytest.mark.asyncio
async def test_quantity_is_string_with_decimal_precision():
    """Webull's v2 spec documents `quantity` as a STRING (decimal
    supported for US equities). If a future PR accidentally passes
    a float, Webull rejects with a parse error."""
    adapter = _adapter_with_instrument("MSFT", "913349712", 380.0)
    await adapter.submit_market_order("MSFT", notional=5, side="BUY")
    _, stock_order = adapter._trade_client.order.place_order_v2_calls[0]
    qty = stock_order["quantity"]
    assert isinstance(qty, str), (
        f"quantity must be a string per Webull v2 docs, got {type(qty)}"
    )
    # Should carry decimal precision (contains '.').
    assert "." in qty, f"expected decimal quantity string, got {qty!r}"
    # Sanity: sub-share for $5 on MSFT @ $380.
    assert 0 < float(qty) < 1.0


@pytest.mark.asyncio
async def test_sell_side_routes_through_qty_decimal_too():
    """SELL intents should also use fractional QTY-decimal path so
    partial-share liquidations work cleanly. SELL uses the DOWNWARD
    slippage band to keep the LIMIT achievable."""
    adapter = _adapter_with_instrument("TSLA", "913303891", 250.0)
    await adapter.submit_market_order("TSLA", notional=3.50, side="SELL")
    _, stock_order = adapter._trade_client.order.place_order_v2_calls[0]
    assert stock_order["side"] == "SELL"
    assert stock_order["entrust_type"] == "QTY"
    assert stock_order["order_type"] == "LIMIT"
    limit_price = float(stock_order["limit_price"])
    # SELL slippage is DOWN from last_price — 50bps below 250 = 248.75.
    assert limit_price < 250.0, (
        f"SELL limit must be below last_price, got {limit_price}"
    )
    qty_val = float(stock_order["quantity"])
    assert 0 < qty_val < 1.0  # $3.50 on TSLA @ $250 → fractional


# ── whole-share path (legacy) still works for explicit qty ─────────


@pytest.mark.asyncio
async def test_qty_path_still_uses_v1_integer_place_order():
    """The whole-share path is still used by reconcile / manual ops.
    Passing `qty` explicitly must hit v1, NOT v2."""
    adapter = _adapter_with_instrument("AAPL", "913256135", 225.0)
    await adapter.submit_market_order("AAPL", qty=2, side="BUY")

    trade = adapter._trade_client
    assert len(trade.order.place_order_v2_calls) == 0, (
        "qty path must NOT call v2 — v2 is for fractional only"
    )
    assert len(trade.order.place_order_calls) == 1
    args, _ = trade.order.place_order_calls[0]
    # Positional signature: account_id, qty, instrument_id, side, ...
    assert args[0] == "SUB123"
    assert args[1] == 2          # integer qty
    assert args[2] == "913256135"


@pytest.mark.asyncio
async def test_qty_below_one_still_blocked_on_legacy_path():
    """The whole-share path still rejects qty<1 — but the error tells
    the caller to use notional instead (so the v2/QTY-decimal path
    takes over)."""
    from shared.broker.webull_caps import WebullCapBlocked
    adapter = _adapter_with_instrument("AAPL", "913256135", 225.0)
    with pytest.raises(WebullCapBlocked) as exc:
        await adapter.submit_market_order("AAPL", qty=0.5, side="BUY")
    msg = str(exc.value)
    assert "QTY_BELOW_ONE" in msg
    assert "notional" in msg, (
        "error message must point the caller at the fractional path"
    )
