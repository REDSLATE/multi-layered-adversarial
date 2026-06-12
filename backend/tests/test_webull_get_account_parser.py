"""Tests for `WebullAdapter.get_account` JSON parsing.

The previous parser looked for camelCase + top-level fields
(`cashBalance`, `buyingPower`, `netLiquidation`). Prod's Webull
OpenAPI v2 returns snake_case + nested under
`account_currency_assets[<USD>]`, and on Individual CASH sub-accounts
the literal `buying_power` field is *0.00* — those accounts spend
`settled_cash` instead. This caused MC to think the operator's $676.68
funded sub had $0 buying power, blocking every BUY behind the cap gate.

These tests pin the new parser to the actual prod response shape so a
future SDK shape regression fails loudly in CI rather than at runtime.
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


@pytest.fixture(autouse=True)
def _reset():
    reset_webull_adapter_for_tests()
    yield
    reset_webull_adapter_for_tests()


class _StubTradeClient:
    """Stub for `TradeClient` so `adapter._trade()` doesn't try to
    construct the real SDK client (which needs `set_stream_logger`
    on the api_client)."""
    class _AccountV2:
        def get_account_balance(self, *_a, **_kw):  # actual call happens
            raise RuntimeError("should not be called — _sdk_call is mocked")
        def get_account_list(self, *_a, **_kw):
            raise RuntimeError("should not be called — _sdk_call is mocked")
    account_v2 = _AccountV2()


def _adapter() -> WebullAdapter:
    a = WebullAdapter(api_client=_StubApiClient(), account_id="SUB123")
    # short-circuit the SDK resolver so get_account doesn't hit the wire.
    a._resolve_account_id = AsyncMock(return_value="SUB123")  # type: ignore[method-assign]
    a._trade_client = _StubTradeClient()  # bypass real TradeClient construction
    return a


def _patch_balance_response(adapter: WebullAdapter, payload: dict) -> None:
    """Make `_sdk_call(account_v2.get_account_balance, …)` return `payload`."""
    class _Res:
        def __init__(self, body):
            self._body = body
        def json(self):
            return self._body
    adapter._sdk_call = AsyncMock(return_value=_Res(payload))  # type: ignore[method-assign]


# ── the actual prod response shape ────────────────────────────────


PROD_CASH_ACCOUNT_PAYLOAD = {
    "total_net_liquidation_value": "677.67",
    "total_cash_balance": "676.68",
    "account_currency_assets": [
        {
            "currency": "USD",
            "cash_balance": "676.68",
            "settled_cash": "676.68",
            "buying_power": "0.00",            # <- cash account: literal 0
            "option_buying_power": "676.68",
            "net_liquidation_value": "677.67",
        }
    ],
}


@pytest.mark.asyncio
async def test_get_account_cash_subaccount_uses_settled_cash():
    """Prod's Individual CASH sub has `buying_power=0.00`; MC must
    fall through to `settled_cash` so the cap gate sees real headroom."""
    adapter = _adapter()
    _patch_balance_response(adapter, PROD_CASH_ACCOUNT_PAYLOAD)

    acct = await adapter.get_account()

    assert acct["cash"] == pytest.approx(676.68)
    assert acct["buying_power"] == pytest.approx(676.68), (
        "cash-account buying_power must coalesce up to settled_cash, "
        "not stay at the literal $0 the broker returns"
    )
    assert acct["equity"] == pytest.approx(677.67)
    assert acct["account_number"] == "SUB123"
    assert acct["paper"] is False


@pytest.mark.asyncio
async def test_get_account_margin_subaccount_uses_buying_power():
    """A Margin sub with non-zero buying_power must KEEP it (don't
    clobber it with settled_cash)."""
    adapter = _adapter()
    _patch_balance_response(
        adapter,
        {
            "total_net_liquidation_value": "2000.00",
            "account_currency_assets": [
                {
                    "currency": "USD",
                    "settled_cash": "500.00",
                    "cash_balance": "500.00",
                    "buying_power": "1500.00",   # margin gives 3:1ish here
                    "net_liquidation_value": "2000.00",
                }
            ],
        },
    )
    acct = await adapter.get_account()
    assert acct["buying_power"] == pytest.approx(1500.00)
    assert acct["cash"] == pytest.approx(500.00)
    assert acct["equity"] == pytest.approx(2000.00)


@pytest.mark.asyncio
async def test_get_account_picks_usd_when_multiple_currencies():
    """Crypto-enabled accounts can list USD + USDT/BTC rows. We must
    pick USD, not whichever happens to be at [0]."""
    adapter = _adapter()
    _patch_balance_response(
        adapter,
        {
            "total_net_liquidation_value": "676.68",
            "account_currency_assets": [
                {
                    "currency": "BTC",
                    "settled_cash": "0.001",
                    "buying_power": "0.001",
                },
                {
                    "currency": "USD",
                    "settled_cash": "676.68",
                    "cash_balance": "676.68",
                    "buying_power": "0.00",
                },
            ],
        },
    )
    acct = await adapter.get_account()
    assert acct["cash"] == pytest.approx(676.68)
    assert acct["buying_power"] == pytest.approx(676.68)


@pytest.mark.asyncio
async def test_get_account_handles_legacy_envelope():
    """Be tolerant of a future SDK build that re-wraps the body in
    {'data': {...}}."""
    adapter = _adapter()
    _patch_balance_response(
        adapter,
        {
            "code": "200",
            "data": PROD_CASH_ACCOUNT_PAYLOAD,
        },
    )
    acct = await adapter.get_account()
    assert acct["cash"] == pytest.approx(676.68)
    assert acct["buying_power"] == pytest.approx(676.68)


@pytest.mark.asyncio
async def test_get_account_handles_camelcase_legacy_shape():
    """If Webull ever ships an alias build with camelCase top-level
    fields, the legacy fallback path must still produce a sane parse."""
    adapter = _adapter()
    _patch_balance_response(
        adapter,
        {
            "cashBalance": "150.00",
            "buyingPower": "150.00",
            "netLiquidation": "150.00",
        },
    )
    acct = await adapter.get_account()
    assert acct["cash"] == pytest.approx(150.00)
    assert acct["buying_power"] == pytest.approx(150.00)
    assert acct["equity"] == pytest.approx(150.00)


@pytest.mark.asyncio
async def test_get_account_empty_response_returns_zeros_not_crash():
    """If Webull hands us a totally empty body (rare, but seen during
    maintenance windows), the parser must return zeros — NEVER crash.
    Returning zeros makes the gate chain fail closed, which is what
    we want."""
    adapter = _adapter()
    _patch_balance_response(adapter, {})
    acct = await adapter.get_account()
    assert acct["cash"] == 0.0
    assert acct["buying_power"] == 0.0
    assert acct["equity"] == 0.0
