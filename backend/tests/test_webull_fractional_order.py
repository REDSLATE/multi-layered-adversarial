"""Tests for the Webull fractional-share path (place_order_v2 + AMOUNT).

Operator directive (2026-02-19): "all tickers can be fractional. I
brought NVDA for a $1 today." The adapter was rejecting every BUY
priced above the $10 cap with `WEBULL_QTY_BELOW_ONE` because the v1
`place_order` path floor-divides notional into integer shares. The
fix routes notional intents through `place_order_v2` with
`entrust_type="AMOUNT"` + `total_cash_amount="<dollars>"`, which
Webull's docs document as the canonical US fractional-share entry
point.

These tests pin:
  * The v2 SDK method is called (not v1).
  * The stock_order dict carries entrust_type=AMOUNT, the correct
    total_cash_amount string, side/symbol/instrument_id/market.
  * High-priced tickers (e.g., NVDA at $140) no longer raise
    WEBULL_QTY_BELOW_ONE — the operator's $1 NVDA case must pass.
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
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("WEBULL_ARMED", "true")
    monkeypatch.setenv("WEBULL_MIN_NOTIONAL_USD", "1.00")
    monkeypatch.setenv("WEBULL_MAX_NOTIONAL_USD", "10.00")
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
async def test_notional_buy_uses_place_order_v2_with_amount_mode():
    """The whole point of the rev: a notional intent must hit v2 +
    entrust_type=AMOUNT — NEVER the v1 integer path."""
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
    assert stock_order["entrust_type"] == "AMOUNT", (
        "fractional path must set entrust_type=AMOUNT"
    )
    assert stock_order["total_cash_amount"] == "1.00", (
        "AMOUNT mode carries the dollar amount as a string with "
        "2-decimal precision per Webull's v2 docs"
    )
    assert stock_order["symbol"] == "NVDA"
    assert stock_order["instrument_id"] == "913355100"
    assert stock_order["side"] == "BUY"
    assert stock_order["order_type"] == "MARKET"
    assert stock_order["time_in_force"] == "DAY"
    assert stock_order["instrument_type"] == "EQUITY"
    assert stock_order["market"] == "US"
    assert stock_order["support_trading_session"] == "CORE"
    # Extended hours doesn't apply to fractional / market orders.
    assert stock_order["extended_hours_trading"] is False

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
async def test_ten_dollar_aapl_uses_amount_mode_not_qty_rounding():
    """A $10 intent on AAPL (~$225) is the worst case the old code
    handled — it bailed with QTY_BELOW_ONE. With v2/AMOUNT mode the
    broker computes the fractional share count itself."""
    adapter = _adapter_with_instrument("AAPL", "913256135", 225.0)
    result = await adapter.submit_market_order("AAPL", notional=10.00, side="BUY")

    trade = adapter._trade_client
    assert len(trade.order.place_order_v2_calls) == 1
    _, stock_order = trade.order.place_order_v2_calls[0]
    assert stock_order["entrust_type"] == "AMOUNT"
    assert stock_order["total_cash_amount"] == "10.00"
    # No integer-qty assertion on the SDK call — AMOUNT mode handles
    # the fractional share calc broker-side.
    assert result["notional"] == 10.00


@pytest.mark.asyncio
async def test_total_cash_amount_is_string_with_two_decimals():
    """Webull's v2 spec documents total_cash_amount as a STRING. If
    a future PR accidentally passes a float, Webull rejects the
    request with a parse error."""
    adapter = _adapter_with_instrument("MSFT", "913349712", 380.0)
    await adapter.submit_market_order("MSFT", notional=5, side="BUY")
    _, stock_order = adapter._trade_client.order.place_order_v2_calls[0]
    cash = stock_order["total_cash_amount"]
    assert isinstance(cash, str), (
        f"total_cash_amount must be a string per Webull v2 docs, got {type(cash)}"
    )
    assert cash == "5.00"


@pytest.mark.asyncio
async def test_sell_side_routes_through_amount_mode_too():
    """SELL intents should also use fractional path so partial-share
    liquidations work cleanly."""
    adapter = _adapter_with_instrument("TSLA", "913303891", 250.0)
    await adapter.submit_market_order("TSLA", notional=3.50, side="SELL")
    _, stock_order = adapter._trade_client.order.place_order_v2_calls[0]
    assert stock_order["side"] == "SELL"
    assert stock_order["entrust_type"] == "AMOUNT"
    assert stock_order["total_cash_amount"] == "3.50"


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
    the caller to use notional instead (so the v2/AMOUNT path takes
    over)."""
    from shared.broker.webull_caps import WebullCapBlocked
    adapter = _adapter_with_instrument("AAPL", "913256135", 225.0)
    with pytest.raises(WebullCapBlocked) as exc:
        await adapter.submit_market_order("AAPL", qty=0.5, side="BUY")
    msg = str(exc.value)
    assert "QTY_BELOW_ONE" in msg
    assert "notional" in msg, (
        "error message must point the caller at the fractional path"
    )
