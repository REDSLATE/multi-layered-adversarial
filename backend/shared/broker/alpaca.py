"""Alpaca paper-trading adapter.

Wraps `alpaca.trading.client.TradingClient` and normalises every response
into the shapes defined by `shared.broker.base`. No alpaca-py types leak
past this module.

Doctrine:
  * `paper=True` is HARD-CODED. Live trading is a different adapter,
    deliberately, so a config flip can't accidentally route real orders.
  * The TradingClient SDK is blocking. We run every call through
    `asyncio.to_thread` so we don't stall FastAPI's event loop.
"""
from __future__ import annotations

import asyncio
from typing import Optional

from alpaca.common.exceptions import APIError
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, OrderStatus, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import (
    GetOrdersRequest,
    LimitOrderRequest,
    MarketOrderRequest,
)

from shared.broker.base import (
    BrokerAccount,
    BrokerAdapter,
    BrokerOrder,
    BrokerPosition,
)


def _to_side(side: str) -> OrderSide:
    s = (side or "").upper()
    if s == "BUY":
        return OrderSide.BUY
    if s == "SELL":
        return OrderSide.SELL
    raise ValueError(f"side must be BUY or SELL; got {side!r}")


def _norm_status(s) -> str:
    if isinstance(s, OrderStatus):
        return s.value
    return str(s) if s is not None else ""


def _to_float(v) -> float:
    if v is None:
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _opt_float(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _opt_iso(v) -> Optional[str]:
    return v.isoformat() if v else None


def _format_order(o) -> BrokerOrder:
    return {
        "order_id": str(o.id),
        "client_order_id": getattr(o, "client_order_id", None),
        "symbol": o.symbol,
        "qty": _to_float(o.qty),
        "notional": _opt_float(getattr(o, "notional", None)),
        "side": (o.side.value if hasattr(o.side, "value") else str(o.side)).upper(),
        "type": (o.order_type.value if hasattr(o.order_type, "value") else str(o.order_type)),
        "limit_price": _opt_float(o.limit_price),
        "time_in_force": (
            o.time_in_force.value if hasattr(o.time_in_force, "value") else str(o.time_in_force)
        ),
        "status": _norm_status(o.status),
        "submitted_at": _opt_iso(getattr(o, "submitted_at", None)),
        "filled_at": _opt_iso(getattr(o, "filled_at", None)),
        "filled_qty": _to_float(getattr(o, "filled_qty", 0)),
        "filled_avg_price": _opt_float(getattr(o, "filled_avg_price", None)),
    }


def _format_position(p) -> BrokerPosition:
    return {
        "symbol": p.symbol,
        "qty": _to_float(p.qty),
        "side": (p.side.value if hasattr(p.side, "value") else str(p.side)).lower(),
        "avg_entry_price": _to_float(p.avg_entry_price),
        "market_value": _to_float(p.market_value),
        "cost_basis": _to_float(p.cost_basis),
        "unrealized_pl": _to_float(p.unrealized_pl),
        "unrealized_plpc": _to_float(p.unrealized_plpc),
        "current_price": _opt_float(getattr(p, "current_price", None)),
    }


class AlpacaPaperAdapter(BrokerAdapter):
    """Paper-trading-only Alpaca adapter."""

    name = "alpaca_paper"
    is_paper = True

    def __init__(self, api_key: str, secret_key: str):
        if not api_key or not secret_key:
            raise ValueError("AlpacaPaperAdapter requires api_key and secret_key")
        # `paper=True` is deliberately hard-coded. This class only ever
        # talks to https://paper-api.alpaca.markets.
        self._client = TradingClient(api_key=api_key, secret_key=secret_key, paper=True)

    # ─── helpers ────────────────────────────────────────────────────

    async def _run(self, fn, *args, **kwargs):
        """Run a blocking SDK call on a worker thread."""
        return await asyncio.to_thread(fn, *args, **kwargs)

    # ─── BrokerAdapter ──────────────────────────────────────────────

    async def ping(self) -> dict:
        acct = await self._run(self._client.get_account)
        return {
            "ok": True,
            "account_number": acct.account_number,
            "status": str(acct.status),
            "equity": _to_float(acct.equity),
            "cash": _to_float(acct.cash),
            "buying_power": _to_float(acct.buying_power),
            "paper": True,
        }

    async def get_account(self) -> BrokerAccount:
        acct = await self._run(self._client.get_account)
        return {
            "account_number": acct.account_number,
            "status": str(acct.status),
            "equity": _to_float(acct.equity),
            "cash": _to_float(acct.cash),
            "buying_power": _to_float(acct.buying_power),
            "daytrade_buying_power": _to_float(getattr(acct, "daytrade_buying_power", 0)),
            "last_equity": _to_float(getattr(acct, "last_equity", 0)),
            "pattern_day_trader": bool(getattr(acct, "pattern_day_trader", False)),
            "paper": True,
        }

    async def submit_market_order(
        self,
        symbol: str,
        qty: Optional[float] = None,
        notional: Optional[float] = None,
        side: str = "BUY",
        client_order_id: Optional[str] = None,
    ) -> BrokerOrder:
        if (qty is None) == (notional is None):
            raise ValueError("supply exactly one of qty or notional")
        kwargs: dict = {
            "symbol": symbol.upper(),
            "side": _to_side(side),
            "time_in_force": TimeInForce.DAY,
        }
        if qty is not None:
            kwargs["qty"] = float(qty)
        else:
            kwargs["notional"] = float(notional)
        if client_order_id:
            kwargs["client_order_id"] = client_order_id

        req = MarketOrderRequest(**kwargs)
        order = await self._run(self._client.submit_order, req)
        return _format_order(order)

    async def submit_limit_order(
        self,
        symbol: str,
        qty: float,
        limit_price: float,
        side: str = "BUY",
        client_order_id: Optional[str] = None,
    ) -> BrokerOrder:
        kwargs: dict = {
            "symbol": symbol.upper(),
            "qty": float(qty),
            "limit_price": float(limit_price),
            "side": _to_side(side),
            "time_in_force": TimeInForce.DAY,
        }
        if client_order_id:
            kwargs["client_order_id"] = client_order_id
        req = LimitOrderRequest(**kwargs)
        order = await self._run(self._client.submit_order, req)
        return _format_order(order)

    async def get_order(self, order_id: str) -> BrokerOrder:
        order = await self._run(self._client.get_order_by_id, order_id)
        return _format_order(order)

    async def list_open_orders(self) -> list[BrokerOrder]:
        req = GetOrdersRequest(status=QueryOrderStatus.OPEN)
        orders = await self._run(self._client.get_orders, req)
        return [_format_order(o) for o in (orders or [])]

    async def cancel_order(self, order_id: str) -> None:
        try:
            await self._run(self._client.cancel_order_by_id, order_id)
        except APIError as e:
            # Alpaca returns 422 for already-filled / closed orders.
            if "422" in str(e):
                raise ValueError(f"order {order_id} is no longer cancelable") from e
            raise

    async def list_positions(self) -> list[BrokerPosition]:
        positions = await self._run(self._client.get_all_positions)
        return [_format_position(p) for p in (positions or [])]

    async def close_position(self, symbol: str) -> BrokerOrder:
        order = await self._run(self._client.close_position, symbol.upper())
        return _format_order(order)
