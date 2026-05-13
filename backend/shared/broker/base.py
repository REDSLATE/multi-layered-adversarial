"""Broker adapter contract.

Every broker MC speaks to (Alpaca paper, Public.com, Kraken later) MUST
expose this interface. The execution router and gate chain consume this
shape; they never reach into a broker SDK directly.

Doctrine:
  * Methods are async. The execution router is async; HTTP I/O to a
    broker should never block the event loop.
  * Return types are plain dicts (TypedDicts here for IDE support).
    No SDK objects leak past the adapter boundary — that keeps the rest
    of the codebase decoupled from any one broker's models.
  * All adapters MUST normalise side as "BUY" / "SELL" uppercase.
  * Adapters report dollars as float USD. Quantities are float (Alpaca
    supports fractional shares for market orders).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional, TypedDict


class BrokerAccount(TypedDict, total=False):
    account_number: str
    status: str
    equity: float
    cash: float
    buying_power: float
    daytrade_buying_power: float
    last_equity: float
    pattern_day_trader: bool
    paper: bool


class BrokerOrder(TypedDict, total=False):
    order_id: str               # broker's id
    client_order_id: Optional[str]  # ours; used for idempotency
    symbol: str
    qty: float
    notional: Optional[float]
    side: str                   # "BUY" | "SELL"
    type: str                   # "market" | "limit"
    limit_price: Optional[float]
    time_in_force: str
    status: str                 # broker-native status string
    submitted_at: Optional[str]
    filled_at: Optional[str]
    filled_qty: float
    filled_avg_price: Optional[float]


class BrokerPosition(TypedDict, total=False):
    symbol: str
    qty: float
    side: str                   # "long" | "short"
    avg_entry_price: float
    market_value: float
    cost_basis: float
    unrealized_pl: float
    unrealized_plpc: float
    current_price: Optional[float]


class BrokerAdapter(ABC):
    """Abstract base. Concrete adapters: AlpacaPaperAdapter, ..."""

    name: str = "abstract"
    is_paper: bool = True

    @abstractmethod
    async def ping(self) -> dict:
        """Cheapest possible round-trip to confirm keys are alive.
        Returns ``{"ok": True, "account_number": "...", "equity": ...}``
        on success; raises on auth/network failure."""

    @abstractmethod
    async def get_account(self) -> BrokerAccount: ...

    @abstractmethod
    async def submit_market_order(
        self,
        symbol: str,
        qty: Optional[float] = None,
        notional: Optional[float] = None,
        side: str = "BUY",
        client_order_id: Optional[str] = None,
    ) -> BrokerOrder:
        """Submit a market day order. Exactly one of `qty` or `notional`
        must be supplied. `notional` requires the broker to support
        dollar-amount orders (Alpaca does)."""

    @abstractmethod
    async def submit_limit_order(
        self,
        symbol: str,
        qty: float,
        limit_price: float,
        side: str = "BUY",
        client_order_id: Optional[str] = None,
    ) -> BrokerOrder: ...

    @abstractmethod
    async def get_order(self, order_id: str) -> BrokerOrder: ...

    @abstractmethod
    async def list_open_orders(self) -> list[BrokerOrder]: ...

    @abstractmethod
    async def cancel_order(self, order_id: str) -> None: ...

    @abstractmethod
    async def list_positions(self) -> list[BrokerPosition]: ...

    @abstractmethod
    async def close_position(self, symbol: str) -> BrokerOrder:
        """Close the open position for `symbol`. Returns the closing
        order receipt (a SELL for a long, a BUY for a short)."""
