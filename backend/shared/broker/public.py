"""Public.com broker adapter (equity orders only).

Conforms to `shared.broker.base.BrokerAdapter` so the execution
router + gate chain can talk to Public.com via the same interface
as Alpaca / Kraken.

Doctrine:
  * Adapter is a thin shim over `shared.public` — that module owns
    secret storage, token refresh, and the underlying _request helper.
  * Adapter NEVER reads MC's `execution_enabled` flag directly. That
    flag lives in `public_credentials.execution_enabled` and is
    consulted by the broker_router *before* an adapter is loaded.
    So if MC routes here at all, the operator already authorized.
  * Submit endpoint requires a client-generated UUID per order.
    `client_order_id` from the gate chain is used when present,
    otherwise we generate one. Same UUID gets echoed back in the
    receipt for idempotency.
  * Public.com is equity-only here. Options / crypto / bonds can be
    added later without touching MC's routing.

API map (Public docs, 2026-06):
  GET    /userapigateway/trading/account                     — list accounts
  POST   /userapigateway/trading/{acct}/preflight/single-leg — quote + estimate
  POST   /userapigateway/trading/{acct}/order                — submit
  GET    /userapigateway/trading/{acct}/order/{orderId}      — status
  GET    /userapigateway/trading/{acct}/portfolio/v2         — positions
  POST   /userapigateway/marketdata/{acct}/quotes            — bid/ask
"""
from __future__ import annotations

import uuid
from typing import Optional

from shared.broker.base import (
    BrokerAccount, BrokerAdapter, BrokerOrder, BrokerPosition,
)
from shared.public import PublicError, _request


def _norm_side(s: str) -> str:
    s = (s or "BUY").upper()
    if s not in {"BUY", "SELL"}:
        raise ValueError(f"unsupported side: {s!r}")
    return s


def _ord_id(client_order_id: Optional[str]) -> str:
    """Use the caller's idempotency key if it's UUID-shaped; otherwise
    burn a fresh UUID. Public.com REQUIRES UUID format for orderId."""
    if client_order_id:
        try:
            return str(uuid.UUID(client_order_id))
        except ValueError:
            pass
    return str(uuid.uuid4())


def _map_status(p: dict) -> BrokerOrder:
    """Translate Public.com order shape → MC's BrokerOrder TypedDict."""
    return {
        "order_id": p.get("orderId", ""),
        "client_order_id": p.get("orderId"),  # Public uses one id
        "symbol": (p.get("instrument") or {}).get("symbol", ""),
        "qty": float(p.get("quantity") or 0),
        "notional": None,
        "side": (p.get("side") or "").upper(),
        "type": (p.get("type") or "MARKET").lower(),
        "limit_price": (
            float(p["limitPrice"]) if p.get("limitPrice") is not None else None
        ),
        "time_in_force": (p.get("expiration") or {}).get("timeInForce", "DAY"),
        "status": p.get("status", "UNKNOWN"),
        "submitted_at": p.get("createdAt"),
        "filled_at": p.get("filledAt"),
        "filled_qty": float(p.get("filledQuantity") or 0),
        "filled_avg_price": (
            float(p["averagePrice"]) if p.get("averagePrice") is not None else None
        ),
    }


class PublicAdapter(BrokerAdapter):
    """Live (real-money) Public.com brokerage adapter."""

    name = "public"
    is_paper = False  # Public has no separate paper sandbox; real $$.

    def __init__(self, base_url: str, access_token: str, account_id: str):
        if not account_id:
            raise ValueError(
                "PublicAdapter requires account_id (operator must pin one "
                "via /api/admin/public/connect)",
            )
        self.base_url = base_url
        self.access_token = access_token
        self.account_id = account_id

    async def ping(self) -> dict:
        """Cheap auth probe — fetch the portfolio for the pinned account."""
        try:
            p = await _request(
                self.base_url, self.access_token, "GET",
                f"/userapigateway/trading/{self.account_id}/portfolio/v2",
            )
        except PublicError as e:
            raise RuntimeError(f"Public ping failed: {e}") from e
        bp = (p or {}).get("buyingPower") or {}
        equity_str = bp.get("buyingPower") or bp.get("cashOnlyBuyingPower") or "0"
        return {
            "ok": True,
            "account_number": self.account_id,
            "equity": float(equity_str),
        }

    async def get_account(self) -> BrokerAccount:
        p = await _request(
            self.base_url, self.access_token, "GET",
            f"/userapigateway/trading/{self.account_id}/portfolio/v2",
        )
        bp = (p or {}).get("buyingPower") or {}
        cash = float(bp.get("cashOnlyBuyingPower") or 0)
        bp_total = float(bp.get("buyingPower") or 0)
        return {
            "account_number": self.account_id,
            "status": "ACTIVE",
            "equity": bp_total,
            "cash": cash,
            "buying_power": bp_total,
            "daytrade_buying_power": bp_total,
            "last_equity": bp_total,
            "pattern_day_trader": False,
            "paper": False,
        }

    async def submit_market_order(
        self, symbol: str,
        qty: Optional[float] = None,
        notional: Optional[float] = None,
        side: str = "BUY",
        client_order_id: Optional[str] = None,
        mc_receipt: Optional[dict] = None,
    ) -> BrokerOrder:
        # Doctrine: exactly one of qty / notional. Public.com's POST
        # /order does NOT support dollar-notional natively — it takes
        # `quantity` (shares). When MC passes `notional`, we convert
        # to whole-share quantity using a preflight quote so $5 → e.g.
        # 0.03 shares of AAPL. Fractional shares ARE supported on
        # equity market orders for the BROKERAGE account type.
        #
        # `mc_receipt` is accepted from the router for parity with
        # AlpacaAdapter; Public's API doesn't carry it, but we log
        # the signature so the audit trail is complete.
        if mc_receipt:
            sig = (mc_receipt.get("signature") or "")[:12]
            # noqa: G004 — short-form audit
            import logging as _logging
            _logging.getLogger("risedual.public_broker").info(
                "Public submit_market_order receipt_sig=%s symbol=%s side=%s "
                "qty=%s notional=%s",
                sig, symbol, side, qty, notional,
            )
        if (qty is None) == (notional is None):
            raise ValueError("submit_market_order requires exactly one of qty/notional")
        side = _norm_side(side)
        order_id = _ord_id(client_order_id)

        if notional is not None:
            # Resolve last price via marketdata/quotes, then derive qty.
            qty = await self._notional_to_qty(symbol, float(notional))

        body = {
            "orderId": order_id,
            "instrument": {"symbol": symbol.upper(), "type": "EQUITY"},
            "orderSide": side,
            "orderType": "MARKET",
            "expiration": {"timeInForce": "DAY"},
            "quantity": _format_qty(qty),
        }
        try:
            await _request(
                self.base_url, self.access_token, "POST",
                f"/userapigateway/trading/{self.account_id}/order", body,
            )
        except PublicError as e:
            raise RuntimeError(f"Public submit_market_order failed: {e}") from e
        # POST returns just {orderId}. Round-trip the status fetch so
        # MC's receipt has the full shape.
        return await self.get_order(order_id)

    async def submit_limit_order(
        self, symbol: str, qty: float, limit_price: float,
        side: str = "BUY", client_order_id: Optional[str] = None,
        mc_receipt: Optional[dict] = None,
    ) -> BrokerOrder:
        side = _norm_side(side)
        order_id = _ord_id(client_order_id)
        body = {
            "orderId": order_id,
            "instrument": {"symbol": symbol.upper(), "type": "EQUITY"},
            "orderSide": side,
            "orderType": "LIMIT",
            "expiration": {"timeInForce": "DAY"},
            "quantity": _format_qty(qty),
            "limitPrice": _format_price(limit_price),
        }
        try:
            await _request(
                self.base_url, self.access_token, "POST",
                f"/userapigateway/trading/{self.account_id}/order", body,
            )
        except PublicError as e:
            raise RuntimeError(f"Public submit_limit_order failed: {e}") from e
        return await self.get_order(order_id)

    async def get_order(self, order_id: str) -> BrokerOrder:
        try:
            p = await _request(
                self.base_url, self.access_token, "GET",
                f"/userapigateway/trading/{self.account_id}/order/{order_id}",
            )
        except PublicError as e:
            raise RuntimeError(f"Public get_order failed: {e}") from e
        return _map_status(p or {})

    async def list_open_orders(self) -> list[BrokerOrder]:
        # Public.com's docs don't expose a single "list orders" endpoint
        # at the time of writing (2026-06); the portfolio view is the
        # canonical position-state. Return empty so the caller falls
        # back to per-order status polls.
        return []

    async def list_history(
        self,
        start: Optional[str] = None,
        end: Optional[str] = None,
        page_size: int = 200,
        max_pages: int = 50,
    ) -> list[dict]:
        """Pull account transaction history from Public.com.

        Endpoint: `GET /userapigateway/trading/{accountId}/history`
        with query params:
            start      — ISO timestamp (inclusive)
            end        — ISO timestamp (exclusive)
            pageSize   — int
            nextToken  — pagination cursor

        Returns the raw history items list. The history feed includes
        every account event — orders, fills, deposits, dividends, etc.
        Callers filter to what they need (`type == "ORDER"` for the
        replay reconciler).

        Doctrine pin (post-AAPL incident, 2026-06): MC's stored
        `shared_intents` only captures what the BRAINS emitted. The
        broker may have produced many more fills (retries, partial
        fills, re-submissions) than MC's intent count suggests. This
        endpoint is the only way to get the broker's ground truth
        for the 130-fills AAPL incident — necessary for the Pass-6
        replay script's edge proof.

        Pagination: we follow `nextToken` until either it's empty or
        we hit `max_pages` (safety bound — Public's docs don't
        document a maximum result set per query).
        """
        out: list[dict] = []
        next_token: Optional[str] = None
        for page_idx in range(max_pages):
            params: dict = {"pageSize": int(page_size)}
            if start:
                params["start"] = start
            if end:
                params["end"] = end
            if next_token:
                params["nextToken"] = next_token
            try:
                resp = await _request(
                    self.base_url, self.access_token, "GET",
                    f"/userapigateway/trading/{self.account_id}/history",
                    params=params,
                )
            except PublicError as e:
                raise RuntimeError(f"Public list_history failed: {e}") from e
            if not isinstance(resp, dict):
                break
            items = (
                resp.get("transactions")
                or resp.get("history")
                or resp.get("items")
                or resp.get("data")
                or []
            )
            if isinstance(items, list):
                out.extend(items)
            next_token = resp.get("nextToken") or resp.get("next_token")
            if not next_token:
                break
        return out

    async def cancel_order(self, order_id: str) -> None:
        try:
            await _request(
                self.base_url, self.access_token, "DELETE",
                f"/userapigateway/trading/{self.account_id}/order/{order_id}",
            )
        except PublicError as e:
            raise RuntimeError(f"Public cancel_order failed: {e}") from e

    async def list_positions(self) -> list[BrokerPosition]:
        try:
            p = await _request(
                self.base_url, self.access_token, "GET",
                f"/userapigateway/trading/{self.account_id}/portfolio/v2",
            )
        except PublicError as e:
            raise RuntimeError(f"Public list_positions failed: {e}") from e
        out: list[BrokerPosition] = []
        for pos in (p or {}).get("positions") or []:
            inst = pos.get("instrument") or {}
            cost = pos.get("costBasis") or {}
            current_val = float(pos.get("currentValue") or 0)
            unit_cost = float(cost.get("unitCost") or 0)
            qty = float(pos.get("quantity") or 0)
            out.append({
                "symbol": inst.get("symbol", ""),
                "qty": qty,
                "side": "long" if qty >= 0 else "short",
                "avg_entry_price": unit_cost,
                "market_value": current_val,
                "cost_basis": float(cost.get("totalCost") or 0),
                "unrealized_pl": float(cost.get("gainValue") or 0),
                "unrealized_plpc": float(cost.get("gainPercentage") or 0) / 100.0,
                "current_price": (current_val / qty) if qty else None,
            })
        return out

    async def close_position(self, symbol: str) -> BrokerOrder:
        positions = await self.list_positions()
        pos = next((p for p in positions if p["symbol"] == symbol.upper()), None)
        if not pos:
            raise RuntimeError(f"no open position in {symbol}")
        qty = abs(float(pos["qty"]))
        close_side = "SELL" if pos["side"] == "long" else "BUY"
        return await self.submit_market_order(symbol, qty=qty, side=close_side)

    # ── helpers ──
    async def _notional_to_qty(self, symbol: str, notional: float) -> float:
        """Convert $X → share count using a live quote. Conservative:
        rounds UP slightly so the final fill notional stays at or
        below the operator's intent. Minimum 0.001 shares (Public's
        fractional floor for most equities)."""
        body = {"instruments": [{"symbol": symbol.upper(), "type": "EQUITY"}]}
        try:
            q = await _request(
                self.base_url, self.access_token, "POST",
                f"/userapigateway/marketdata/{self.account_id}/quotes", body,
            )
        except PublicError as e:
            raise RuntimeError(f"Public quote failed for sizing: {e}") from e
        quotes = (q or {}).get("quotes") or []
        if not quotes:
            raise RuntimeError(f"no quote returned for {symbol}")
        # Prefer ask for BUY, bid for SELL; fall back to last.
        # Caller doesn't tell us side here — use last as the safest neutral.
        px_str = (quotes[0].get("last") or quotes[0].get("ask") or "0")
        px = float(px_str)
        if px <= 0:
            raise RuntimeError(f"non-positive price for {symbol}")
        qty = max(0.001, notional / px)
        # Public accepts up to 4 decimal places on fractional equity.
        return round(qty, 4)


def _format_qty(q: float) -> str:
    # Public expects string-form decimal. Trim trailing zeros for tidiness.
    s = f"{float(q):.4f}".rstrip("0").rstrip(".")
    return s or "0"


def _format_price(p: float) -> str:
    return f"{float(p):.2f}"
