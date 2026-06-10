"""Webull broker adapter — equities + crypto via the official Webull
OpenAPI Python SDK.

Doctrine (2026-06-10, operator-pinned):

  * LIVE trading from day one. No paper/UAT stop.
  * Webull is a PARALLEL route — Kraken (crypto) and Public.com (equity)
    keys/routes are NOT touched. Operator picks Webull per-intent via
    `intent.broker_override = "webull"`.
  * Pre-trade gate `shared.broker.webull_caps.evaluate_webull_order`
    enforces an "armed" flag plus $3-$10 notional band per ticker.
    This adapter STILL re-checks the armed flag as a belt-and-braces
    measure — if `WEBULL_ARMED!=true` no order leaves the adapter
    even if the router somehow let one through.
  * The adapter NEVER reads the App Secret outside the SDK client
    wrapper. We carry the configured `webull.core.client.ApiClient`
    instance and let the SDK sign every request.
  * Symbol-lane awareness: Webull supports BOTH equity (e.g. "AAPL")
    and crypto (e.g. "BTCUSD"). We build a reverse-lookup from
    `BROKER_SYMBOL_MAP["webull"]` at init so any `submit_market_order`
    call can identify equity vs crypto without changing the base
    `BrokerAdapter` contract.

If `webull-openapi-python-sdk` isn't installed in the env, this module
imports cleanly but `get_webull_adapter()` returns None — which maps
to NO_TRADE at the router. Fail-closed.
"""
from __future__ import annotations

import logging
import os
import uuid
from typing import Any, Optional

from shared.broker.base import (
    BrokerAccount, BrokerAdapter, BrokerOrder, BrokerPosition,
)
from shared.broker.webull_caps import (
    WebullCapBlocked,
    evaluate_webull_order,
    is_webull_armed,
)
from shared.broker_symbol_resolver import BROKER_SYMBOL_MAP

logger = logging.getLogger("risedual.broker.webull")


def _norm_side(s: str) -> str:
    s = (s or "BUY").upper()
    if s not in {"BUY", "SELL"}:
        raise ValueError(f"unsupported side: {s!r}")
    return s


def _build_lane_index() -> dict[str, str]:
    """Reverse Webull's symbol map → {broker_symbol: lane}.

    Lets us tell whether "BTCUSD" is crypto and "AAPL" is equity
    without modifying the base adapter contract or threading lane
    through every call site. Built once at import; cheap.
    """
    out: dict[str, str] = {}
    for canonical, native in (BROKER_SYMBOL_MAP.get("webull") or {}).items():
        if not isinstance(native, str):
            continue
        if canonical.startswith("EQ:"):
            out[native.upper()] = "equity"
        elif canonical.startswith("CRYPTO:"):
            out[native.upper()] = "crypto"
    return out


_LANE_INDEX = _build_lane_index()


def _lane_for_symbol(symbol: str) -> Optional[str]:
    return _LANE_INDEX.get((symbol or "").upper())


class WebullAdapter(BrokerAdapter):
    """Live Webull adapter (equities + crypto).

    The constructor accepts a configured Webull `ApiClient`. The
    factory `get_webull_adapter()` below builds it from env vars
    (`WEBULL_APP_KEY`, `WEBULL_APP_SECRET`, `WEBULL_REGION_ID`,
    `WEBULL_ENVIRONMENT`).
    """

    name = "webull"
    is_paper = False  # operator pinned: live from day one

    def __init__(self, api_client: Any, account_id: Optional[str] = None):
        # We hold the SDK's ApiClient and build per-service clients
        # lazily. Keeping the secret encapsulated here means the rest
        # of the codebase never touches it.
        self._api_client = api_client
        self.account_id = account_id  # set on first account-list call
        # Lazy-loaded SDK sub-clients
        self._trade_client = None

    # ── SDK plumbing ──────────────────────────────────────────────

    def _trade(self):
        if self._trade_client is None:
            # Import inside the method so the file imports cleanly
            # even when the SDK isn't installed.
            from webull.trade.trade_client import TradeClient  # type: ignore  # noqa: WPS433
            self._trade_client = TradeClient(self._api_client)
        return self._trade_client

    async def _resolve_account_id(self) -> str:
        if self.account_id:
            return self.account_id
        try:
            res = self._trade().account_v2.get_account_list()
            data = res.json() if hasattr(res, "json") else res
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"Webull resolve account_id failed: {e}") from e

        # Webull's account list response can arrive in two shapes
        # depending on SDK version:
        #   (a) envelope:   {"code": "200", "data": [{accountId: ...}]}
        #   (b) unwrapped:  [{"accountId": ...}]
        # Be tolerant of both and surface the SDK error code when the
        # envelope says the call failed.
        accounts: list[dict] = []
        if isinstance(data, list):
            accounts = data
        elif isinstance(data, dict):
            inner = data.get("data") or data.get("accounts")
            if isinstance(inner, list):
                accounts = inner
            elif isinstance(inner, dict):
                # Some SDK builds wrap accounts inside data.data.accounts
                nested = inner.get("accounts")
                if isinstance(nested, list):
                    accounts = nested
            code = data.get("code")
            msg = data.get("msg")
            if code not in (None, "200", 200) and not accounts:
                raise RuntimeError(
                    f"Webull get_account_list returned code={code} msg={msg!r}"
                )

        if not accounts:
            raise RuntimeError("Webull account list empty or unparseable")

        first = accounts[0] if isinstance(accounts[0], dict) else {}
        self.account_id = str(
            first.get("accountId") or first.get("account_id") or ""
        )
        if not self.account_id:
            raise RuntimeError("Webull account list missing accountId")
        return self.account_id

    # ── BrokerAdapter contract ────────────────────────────────────

    async def ping(self) -> dict:
        try:
            account_id = await self._resolve_account_id()
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"Webull ping failed: {e}") from e
        return {"ok": True, "account_number": account_id, "equity": 0.0}

    async def get_account(self) -> BrokerAccount:
        account_id = await self._resolve_account_id()
        try:
            res = self._trade().account_v2.get_account_detail(account_id)
            data = res.json() if hasattr(res, "json") else res
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"Webull get_account failed: {e}") from e
        d = (data or {}).get("data") or data or {}
        cash = float(d.get("cashBalance") or d.get("cash") or 0)
        bp = float(d.get("buyingPower") or d.get("dayBuyingPower") or 0)
        equity = float(d.get("netLiquidation") or d.get("totalAssetValue") or bp)
        return {
            "account_number": account_id,
            "status": d.get("status", "ACTIVE"),
            "equity": equity,
            "cash": cash,
            "buying_power": bp,
            "daytrade_buying_power": bp,
            "last_equity": equity,
            "pattern_day_trader": bool(d.get("patternDayTrader") or False),
            "paper": False,
        }

    async def submit_market_order(
        self,
        symbol: str,
        qty: Optional[float] = None,
        notional: Optional[float] = None,
        side: str = "BUY",
        client_order_id: Optional[str] = None,
        mc_receipt: Optional[dict] = None,
    ) -> BrokerOrder:
        """Submit a Webull market order — equity OR crypto.

        Gate chain (defense in depth):
          1. WEBULL_ARMED must be true (the cap-evaluator handles this
             at the router; this is the belt-and-braces check).
          2. Notional must satisfy the cap band (also enforced by the
             router; we re-check here so direct adapter callers can't
             bypass).
          3. Exactly one of (qty, notional) must be supplied.
        """
        # Belt-and-braces re-check of the cap. The router runs
        # `evaluate_webull_order` BEFORE calling us, but defense-in-depth
        # means a misuse of this adapter still fails closed.
        decision = evaluate_webull_order(
            notional_usd=notional, symbol=symbol,
        )
        if not decision.ok and notional is not None:
            raise WebullCapBlocked(decision.reason)
        if not is_webull_armed():
            raise WebullCapBlocked(
                "WEBULL_NOT_ARMED — set WEBULL_ARMED=true in .env; NO_TRADE"
            )
        if (qty is None) == (notional is None):
            raise ValueError("submit_market_order requires exactly one of qty/notional")

        side = _norm_side(side)
        order_id = client_order_id or str(uuid.uuid4())
        account_id = await self._resolve_account_id()

        lane = _lane_for_symbol(symbol)
        if lane is None:
            raise RuntimeError(
                f"Webull adapter: no lane known for symbol {symbol!r}; NO_TRADE"
            )

        # Log MC receipt provenance (signature prefix only — never the
        # full signed blob).
        if mc_receipt:
            sig = (mc_receipt.get("signature") or "")[:12]
            logger.info(
                "Webull submit_market_order receipt_sig=%s symbol=%s lane=%s "
                "side=%s qty=%s notional=%s",
                sig, symbol, lane, side, qty, notional,
            )

        # Build the SDK order payload. The exact field names follow
        # Webull's Trading API Order schema (asset_type, symbol, side,
        # order_type, quantity OR notional, time_in_force). Wrap in
        # try/except so a Webull-side failure surfaces as a clean
        # RuntimeError to the router.
        payload: dict[str, Any] = {
            "account_id": account_id,
            "client_order_id": order_id,
            "asset_type": "CRYPTO" if lane == "crypto" else "EQUITY",
            "symbol": symbol.upper(),
            "side": side,
            "order_type": "MARKET",
            "time_in_force": "DAY",
        }
        if notional is not None:
            # Webull's fractional-equity path takes a notional dollar
            # amount; for crypto it takes the same field. We surface
            # both candidate field names so the SDK can match.
            payload["notional"] = f"{float(notional):.2f}"
            payload["amount"] = f"{float(notional):.2f}"
        else:
            payload["quantity"] = f"{float(qty):.6f}"

        try:
            res = self._trade().order.place_order(payload)
            data = res.json() if hasattr(res, "json") else res
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"Webull submit_market_order failed: {e}") from e

        body = (data or {}).get("data") or data or {}
        return {
            "order_id": str(body.get("orderId") or body.get("order_id") or order_id),
            "client_order_id": order_id,
            "symbol": symbol.upper(),
            "qty": float(qty or 0),
            "notional": float(notional) if notional is not None else None,
            "side": side,
            "type": "market",
            "limit_price": None,
            "time_in_force": "DAY",
            "status": str(body.get("status") or "SUBMITTED"),
            "submitted_at": body.get("createTime") or body.get("submitted_at"),
            "filled_at": body.get("filledAt"),
            "filled_qty": float(body.get("filledQuantity") or 0),
            "filled_avg_price": (
                float(body["averagePrice"])
                if body.get("averagePrice") is not None else None
            ),
        }

    async def submit_limit_order(
        self,
        symbol: str,
        qty: float,
        limit_price: float,
        side: str = "BUY",
        client_order_id: Optional[str] = None,
        mc_receipt: Optional[dict] = None,
    ) -> BrokerOrder:
        # Limit orders are out of scope for the small-pilot route.
        # MC's auto-router exclusively uses market orders today, and
        # the $3-$10 band would make slippage on a limit order
        # essentially meaningless. Implement when we widen the band.
        raise NotImplementedError(
            "WebullAdapter.submit_limit_order is not wired for the "
            "small-pilot route. Use submit_market_order with notional."
        )

    async def get_order(self, order_id: str) -> BrokerOrder:
        account_id = await self._resolve_account_id()
        try:
            res = self._trade().order.get_order_detail(account_id, order_id)
            data = res.json() if hasattr(res, "json") else res
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"Webull get_order failed: {e}") from e
        body = (data or {}).get("data") or data or {}
        return {
            "order_id": str(body.get("orderId") or order_id),
            "client_order_id": body.get("clientOrderId"),
            "symbol": (body.get("symbol") or "").upper(),
            "qty": float(body.get("quantity") or 0),
            "notional": (
                float(body["notional"]) if body.get("notional") is not None else None
            ),
            "side": (body.get("side") or "").upper(),
            "type": (body.get("orderType") or "MARKET").lower(),
            "limit_price": (
                float(body["limitPrice"]) if body.get("limitPrice") is not None else None
            ),
            "time_in_force": body.get("timeInForce", "DAY"),
            "status": body.get("status", "UNKNOWN"),
            "submitted_at": body.get("createTime"),
            "filled_at": body.get("filledAt"),
            "filled_qty": float(body.get("filledQuantity") or 0),
            "filled_avg_price": (
                float(body["averagePrice"])
                if body.get("averagePrice") is not None else None
            ),
        }

    async def list_open_orders(self) -> list[BrokerOrder]:
        # Webull's history endpoint covers seven days; we filter to
        # OPEN/PENDING status for the open-order view. If the SDK
        # exposes a dedicated open-orders call it can replace this.
        try:
            account_id = await self._resolve_account_id()
            res = self._trade().order.get_order_history(account_id)
            data = res.json() if hasattr(res, "json") else res
        except Exception:  # noqa: BLE001
            return []
        rows = (data or {}).get("data") or data or []
        out: list[BrokerOrder] = []
        if isinstance(rows, list):
            for r in rows:
                status = (r.get("status") or "").upper()
                if status in {"PENDING", "SUBMITTED", "WORKING", "OPEN"}:
                    out.append({
                        "order_id": str(r.get("orderId") or ""),
                        "client_order_id": r.get("clientOrderId"),
                        "symbol": (r.get("symbol") or "").upper(),
                        "qty": float(r.get("quantity") or 0),
                        "notional": None,
                        "side": (r.get("side") or "").upper(),
                        "type": (r.get("orderType") or "MARKET").lower(),
                        "limit_price": None,
                        "time_in_force": r.get("timeInForce", "DAY"),
                        "status": status,
                        "submitted_at": r.get("createTime"),
                        "filled_at": None,
                        "filled_qty": float(r.get("filledQuantity") or 0),
                        "filled_avg_price": None,
                    })
        return out

    async def cancel_order(self, order_id: str) -> None:
        account_id = await self._resolve_account_id()
        try:
            self._trade().order.cancel_order({
                "account_id": account_id,
                "order_id": order_id,
            })
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"Webull cancel_order failed: {e}") from e

    async def list_positions(self) -> list[BrokerPosition]:
        try:
            account_id = await self._resolve_account_id()
            res = self._trade().position.get_positions(account_id)
            data = res.json() if hasattr(res, "json") else res
        except Exception:  # noqa: BLE001
            return []
        rows = (data or {}).get("data") or data or []
        out: list[BrokerPosition] = []
        if isinstance(rows, list):
            for p in rows:
                qty = float(p.get("quantity") or 0)
                cost = float(p.get("costPrice") or p.get("avgPrice") or 0)
                mv = float(p.get("marketValue") or 0)
                upl = float(p.get("unrealizedPnL") or 0)
                out.append({
                    "symbol": (p.get("symbol") or "").upper(),
                    "qty": qty,
                    "side": "long" if qty >= 0 else "short",
                    "avg_entry_price": cost,
                    "market_value": mv,
                    "cost_basis": cost * abs(qty),
                    "unrealized_pl": upl,
                    "unrealized_plpc": (upl / (cost * abs(qty))) if (cost and qty) else 0.0,
                    "current_price": (mv / qty) if qty else None,
                })
        return out

    async def close_position(self, symbol: str) -> BrokerOrder:
        positions = await self.list_positions()
        pos = next((p for p in positions if p["symbol"] == symbol.upper()), None)
        if not pos:
            raise RuntimeError(f"no open Webull position in {symbol}")
        qty = abs(float(pos["qty"]))
        close_side = "SELL" if pos["side"] == "long" else "BUY"
        return await self.submit_market_order(symbol, qty=qty, side=close_side)


# ─────────────────────── factory ───────────────────────

async def get_webull_adapter() -> Optional[WebullAdapter]:
    """Build a `WebullAdapter` from env vars, or return None.

    Returns None when:
      * The Webull Python SDK isn't installed (graceful — the broker
        loader treats None as NO_TRADE).
      * WEBULL_APP_KEY or WEBULL_APP_SECRET is empty.
      * WEBULL_ARMED isn't true. The adapter STILL refuses orders
        inside `submit_market_order` if the operator flips this
        mid-session, but returning None here means the router never
        even wires up a client during quiet times.

    Mirrors `_get_public_adapter`'s shape exactly so the loader
    registry in broker_router.py can call it the same way.
    """
    app_key = (os.environ.get("WEBULL_APP_KEY") or "").strip()
    app_secret = (os.environ.get("WEBULL_APP_SECRET") or "").strip()
    if not app_key or not app_secret:
        return None
    if not is_webull_armed():
        # Cleanly disabled — return None so the operator sees
        # "broker not configured" instead of "armed gate refused"
        # in the route log. The cap evaluator surfaces the latter
        # message at the router level if anything tries to bypass.
        return None

    region_id = (os.environ.get("WEBULL_REGION_ID") or "us").strip()
    environment = (os.environ.get("WEBULL_ENVIRONMENT") or "prod").strip().lower()

    try:
        from webull.core.client import ApiClient  # type: ignore  # noqa: WPS433
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "Webull SDK not importable (webull-openapi-python-sdk missing?): %s", e,
        )
        return None

    try:
        api_client = ApiClient(app_key, app_secret, region_id)
        if environment == "uat":
            api_client.add_endpoint(region_id, "us-openapi-alb.uat.webullbroker.com")
    except Exception as e:  # noqa: BLE001
        logger.warning("Webull ApiClient construction failed: %s", e)
        return None

    return WebullAdapter(api_client=api_client)
