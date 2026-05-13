"""Unit tests for the Alpaca broker adapter (mocked SDK)."""
import asyncio
import pytest
from unittest.mock import MagicMock, patch
from types import SimpleNamespace

from shared.broker.alpaca import AlpacaPaperAdapter, _to_side, _format_order
from alpaca.trading.enums import OrderStatus


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if asyncio.get_event_loop().is_closed() is False and not asyncio.get_event_loop().is_running() else asyncio.run(coro)


def _mock_order(**overrides):
    base = dict(
        id="ord-123",
        client_order_id="mc-abc-def",
        symbol="AAPL",
        qty=1.0,
        notional=None,
        side=SimpleNamespace(value="buy"),
        order_type=SimpleNamespace(value="market"),
        limit_price=None,
        time_in_force=SimpleNamespace(value="day"),
        status=OrderStatus.ACCEPTED,
        submitted_at=None,
        filled_at=None,
        filled_qty=0,
        filled_avg_price=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_to_side_normalises():
    from alpaca.trading.enums import OrderSide
    assert _to_side("buy") == OrderSide.BUY
    assert _to_side("Sell") == OrderSide.SELL
    with pytest.raises(ValueError):
        _to_side("hold")


def test_format_order_shape():
    o = _mock_order()
    f = _format_order(o)
    assert f["order_id"] == "ord-123"
    assert f["symbol"] == "AAPL"
    assert f["side"] == "BUY"
    assert f["status"] == "accepted"
    assert f["type"] == "market"


def test_init_requires_keys():
    with pytest.raises(ValueError):
        AlpacaPaperAdapter("", "")


def test_ping_returns_account_dict():
    with patch("shared.broker.alpaca.TradingClient") as TC:
        client = MagicMock()
        client.get_account.return_value = SimpleNamespace(
            account_number="PA9999",
            status="ACTIVE",
            equity=100000.5,
            cash=50000.0,
            buying_power=200000.0,
        )
        TC.return_value = client
        adapter = AlpacaPaperAdapter("PKabc", "secretdef")
        out = asyncio.run(adapter.ping())
        assert out["ok"] is True
        assert out["account_number"] == "PA9999"
        assert out["equity"] == 100000.5
        assert out["paper"] is True


def test_submit_market_order_requires_exactly_one_of_qty_notional():
    with patch("shared.broker.alpaca.TradingClient") as TC:
        TC.return_value = MagicMock()
        adapter = AlpacaPaperAdapter("PKabc", "secretdef")
        with pytest.raises(ValueError):
            asyncio.run(adapter.submit_market_order("AAPL", side="BUY"))  # neither
        with pytest.raises(ValueError):
            asyncio.run(adapter.submit_market_order("AAPL", qty=1, notional=10, side="BUY"))  # both


def test_submit_market_order_uses_notional_when_supplied():
    with patch("shared.broker.alpaca.TradingClient") as TC:
        client = MagicMock()
        client.submit_order.return_value = _mock_order(symbol="TSLA", side=SimpleNamespace(value="buy"))
        TC.return_value = client
        adapter = AlpacaPaperAdapter("PKabc", "secretdef")
        out = asyncio.run(adapter.submit_market_order("tsla", notional=10.0, side="BUY"))
        assert out["symbol"] == "TSLA"
        called_with = client.submit_order.call_args.args[0]
        assert getattr(called_with, "notional", None) == 10.0
        assert getattr(called_with, "qty", None) is None
