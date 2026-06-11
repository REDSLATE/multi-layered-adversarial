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

2026-02-19 — Event-loop safety:
  Every Webull SDK call this adapter makes is a SYNCHRONOUS blocking
  HTTPS request. Calling them directly from `async def` methods
  starves the FastAPI event loop while each request is in flight,
  which on prod caused the pod to accumulate Cloudflare 520s after
  the auto-router took ~15 minutes' worth of ticks. Every SDK call
  is now dispatched through `asyncio.get_running_loop().run_in_executor`
  so the event loop stays responsive while the SDK does its
  synchronous HTTPS round-trip on a worker thread.

2026-02-19 (rev 2) — Adapter singleton + SDK log quiet:
  The Webull SDK caches its OAuth token on the ApiClient. Constructing
  a fresh ApiClient per order (the previous pattern) burned the
  token cache, sending the SDK into a `_check_token_enable result is
  False` hot loop that wedged the executor thread and produced an
  HTTP 502 even with the executor wrapping in place. The adapter is
  now a process-wide singleton (`_ADAPTER`, see factory below) so the
  token stays warm across orders. The same module also raises the
  Webull SDK's `client_initializer` logger to WARNING so the INFO-level
  token-check chatter doesn't drown the supervisor logs.

If `webull-openapi-python-sdk` isn't installed in the env, this module
imports cleanly but `get_webull_adapter()` returns None — which maps
to NO_TRADE at the router. Fail-closed.
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
import uuid
from typing import Any, Optional

# 2026-02-19 — Quiet the Webull SDK's INFO-level token-check chatter
# so the operator can actually see request/response lines in the
# supervisor logs. The SDK logs `_check_token_enable result is False`
# every time it probes its internal token cache — which is many
# times per order. Raising the threshold to WARNING preserves real
# errors while removing the noise.
for _noisy in (
    "webull.core.http.initializer.client_initializer",
    "webull.core.http.initializer",
    "webull.core.client",
):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

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
    """Return 'equity' / 'crypto' / None for a Webull-native symbol.

    Pure, sync, no I/O. Two-tier lookup (2026-06-10, operator wants
    Webull to cover the full patterns_universe without manual map
    upkeep):
      1. Static `_LANE_INDEX` built from BROKER_SYMBOL_MAP["webull"]
         (explicit operator overrides — always wins).
      2. Heuristic fallback for the order path: 6+ letter symbol
         ending in USD/USDT → crypto; anything purely alphabetic and
         1-5 chars → equity. Returns None when undecidable so the
         adapter fails closed.
    """
    sym = (symbol or "").upper()
    if not sym:
        return None
    cached = _LANE_INDEX.get(sym)
    if cached:
        return cached
    # Heuristic — crypto pairs on Webull end in USD/USDT.
    if sym.endswith("USDT") and len(sym) >= 7 and sym[:-4].isalpha():
        return "crypto"
    if sym.endswith("USD") and len(sym) >= 6 and sym[:-3].isalpha():
        return "crypto"
    if sym.isalpha() and 1 <= len(sym) <= 5:
        return "equity"
    return None


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
        # Per-symbol instrument_id + last_price + fractionable cache.
        # Webull's place_order takes an `instrument_id` (numeric), not
        # a ticker; the lookup is the same one used for quotes and is
        # safe to cache for the life of the singleton.
        self._instrument_cache: dict[str, tuple[str, float, bool]] = {}

    # ── SDK plumbing ──────────────────────────────────────────────

    def _trade(self):
        if self._trade_client is None:
            # Import inside the method so the file imports cleanly
            # even when the SDK isn't installed.
            from webull.trade.trade_client import TradeClient  # type: ignore  # noqa: WPS433
            self._trade_client = TradeClient(self._api_client)
        return self._trade_client

    async def _sdk_call(self, fn, *args, **kwargs):
        """Dispatch a synchronous SDK method on a thread executor.

        2026-02-19: every SDK method (`get_account_list`,
        `get_account_detail`, `place_order`, etc.) is a blocking
        HTTPS request. Calling them from an `async def` method
        without `run_in_executor` blocks the event loop for the
        duration of the round-trip — under the auto-router's 5-per-
        tick load this caused the prod pod to accumulate gateway
        timeouts and crash after ~15 minutes. This helper isolates
        the sync work on the default thread pool so the loop stays
        responsive.

        The SDK does not provide an async interface; the worker
        thread is the cheapest correct fix.
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: fn(*args, **kwargs))

    async def _resolve_account_id(self) -> str:
        if self.account_id:
            return self.account_id
        # 2026-02-19 — Operator override. A real Webull profile has
        # multiple sub-accounts (Margin, Cash, Events, Futures, …).
        # `accounts[0]` may not be the funded one (operator screenshot
        # showed Total=$777.68 but Margin sub=$0, Cash sub=$0 — the
        # SDK's first row was the Margin account). If the operator
        # pins `WEBULL_ACCOUNT_ID` we use it as-is and skip the
        # auto-pick logic.
        pinned = (os.environ.get("WEBULL_ACCOUNT_ID") or "").strip()
        if pinned:
            self.account_id = pinned
            return self.account_id
        try:
            res = await self._sdk_call(self._trade().account_v2.get_account_list)
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

        # 2026-02-19 — Picking logic, in priority order:
        #   1. CASH account (operator's funded sub-account on Webull
        #      retail profiles — confirmed via direct screenshot
        #      cross-check).
        #   2. MARGIN account (next-most-common funded sub).
        #   3. First account in the list (back-compat fallback).
        # The operator can override with WEBULL_ACCOUNT_ID at any time
        # if Webull adds new sub-account types or the funding moves.
        def _type(a: dict) -> str:
            t = a.get("accountType") or a.get("account_type") or ""
            return str(t).upper()

        picked = None
        for a in accounts:
            if _type(a) == "CASH":
                picked = a
                break
        if picked is None:
            for a in accounts:
                if _type(a) == "MARGIN":
                    picked = a
                    break
        if picked is None:
            picked = accounts[0] if isinstance(accounts[0], dict) else {}

        self.account_id = str(
            picked.get("accountId") or picked.get("account_id") or ""
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
        # 2026-02-19: real SDK exposes `get_account_balance`, not
        # `get_account_detail`. Method name corrected; response shape
        # is the same JSON envelope.
        account_id = await self._resolve_account_id()
        try:
            res = await self._sdk_call(
                self._trade().account_v2.get_account_balance, account_id,
            )
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

    async def _resolve_instrument_id(self, symbol: str) -> tuple[str, float, bool]:
        """Look up a Webull instrument_id + last price + fractionable flag.

        2026-02-19: Webull's `place_order` takes an `instrument_id`
        (numeric internal ID), NOT a ticker symbol. We resolve the
        symbol via the quotes-side `get_quotes_client().instrument()`
        helper which already runs sync — wrap in `_sdk_call` so we
        stay off the event loop. Cached per-symbol on the singleton
        so repeated orders for the same ticker don't re-hit the API.

        Returns (instrument_id, last_price, fractionable).
        """
        sym_u = (symbol or "").upper().strip()
        cached = self._instrument_cache.get(sym_u)
        if cached is not None:
            return cached

        from shared.market_data.webull_quotes import get_quotes_client  # noqa: WPS433
        client = get_quotes_client()

        def _lookup():
            instr = client.instrument(sym_u) or {}
            snap = client.equity_snapshot(sym_u) or {}
            iid = str(instr.get("instrument_id") or instr.get("instrumentId") or "")
            price = float(snap.get("price") or snap.get("ask") or 0.0) or 0.0
            frac = bool(instr.get("fractionable") or False)
            return iid, price, frac

        try:
            iid, price, frac = await self._sdk_call(_lookup)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(
                f"Webull instrument lookup failed for {symbol!r}: {e}"
            ) from e
        if not iid:
            raise RuntimeError(
                f"Webull instrument_id not found for {symbol!r}; NO_TRADE"
            )
        if price <= 0.0:
            raise RuntimeError(
                f"Webull last-price unavailable for {symbol!r}; NO_TRADE"
            )
        self._instrument_cache[sym_u] = (iid, price, frac)
        return iid, price, frac

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

        2026-02-19 (rev 2): rewritten against the actual installed
        Webull SDK signature. The previous implementation passed a
        dict payload to `place_order(...)` which takes positional
        args — the SDK raised TypeError inside the executor thread,
        the thread wedged, the request stalled past the gateway
        timeout and surfaced as HTTP 502 on the dashboard.

        Real SDK signature:
          place_order(account_id, qty, instrument_id, side,
                      client_order_id, order_type,
                      extended_hours_trading, tif, limit_price=None,
                      stop_price=None, ...)

        Webull standard API supports WHOLE-SHARE QTY only — no
        fractional. We convert the requested notional to qty via
        `floor(notional / last_price)` and re-validate the resulting
        notional against the cap band so the actual dollars spent
        still respect the operator's $3-$10 ceiling (a high-priced
        ticker yields qty=0 → NO_TRADE).
        """
        # Belt-and-braces re-check of the cap.
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

        side_str = _norm_side(side)
        order_id = client_order_id or str(uuid.uuid4())
        # Webull's client_order_id is capped at 40 chars (per SDK
        # docstring). UUID4 is 36 chars — fits. Truncate defensively.
        if len(order_id) > 40:
            order_id = order_id[:40]
        account_id = await self._resolve_account_id()

        lane = _lane_for_symbol(symbol)
        if lane is None:
            raise RuntimeError(
                f"Webull adapter: no lane known for symbol {symbol!r}; NO_TRADE"
            )

        # Resolve the instrument_id + last price (cached).
        instrument_id, last_price, fractionable = await self._resolve_instrument_id(symbol)

        # Convert notional → integer qty. Webull's place_order takes
        # `qty: integer`; fractional is not supported via this entry
        # point. If qty<1 the notional can't fit one share at current
        # price — NO_TRADE with a readable reason.
        if qty is None:
            calc_qty = int(float(notional) // last_price) if last_price > 0 else 0
            if calc_qty < 1:
                raise WebullCapBlocked(
                    f"WEBULL_QTY_BELOW_ONE — {symbol} last=${last_price:.2f} "
                    f"× 1 = ${last_price:.2f} which exceeds notional "
                    f"${float(notional):.2f}. Webull does not support "
                    f"fractional shares via this adapter; pick a lower-"
                    f"priced ticker or raise the notional cap."
                )
            qty_int = calc_qty
            # Recompute effective notional and re-check the cap. The
            # actual dollars spent at execution may exceed the
            # requested notional because we're snapping up to a whole
            # share — re-validate against the band so a runaway price
            # spike can't blow past the cap.
            effective_notional = qty_int * last_price
            decision2 = evaluate_webull_order(
                notional_usd=effective_notional, symbol=symbol,
            )
            if not decision2.ok:
                raise WebullCapBlocked(
                    f"{decision2.reason} (effective={effective_notional:.2f} "
                    f"from {qty_int} × ${last_price:.2f})"
                )
        else:
            qty_int = int(float(qty))
            if qty_int < 1:
                raise WebullCapBlocked(
                    f"WEBULL_QTY_BELOW_ONE — qty={qty} < 1; Webull requires "
                    f"whole shares via this adapter."
                )

        # Log MC receipt provenance (signature prefix only).
        if mc_receipt:
            sig = (mc_receipt.get("signature") or "")[:12]
            logger.info(
                "Webull submit_market_order receipt_sig=%s symbol=%s "
                "instrument_id=%s lane=%s side=%s qty=%s notional=%s "
                "last_price=%s",
                sig, symbol, instrument_id, lane, side_str, qty_int,
                notional, last_price,
            )
        else:
            logger.info(
                "Webull submit_market_order symbol=%s instrument_id=%s "
                "lane=%s side=%s qty=%s notional=%s last_price=%s",
                symbol, instrument_id, lane, side_str, qty_int,
                notional, last_price,
            )

        # Resolve SDK enums.
        from webull.trade.common.order_side import OrderSide  # noqa: WPS433
        from webull.trade.common.order_tif import OrderTIF  # noqa: WPS433
        from webull.trade.common.order_type import OrderType  # noqa: WPS433

        sdk_side = OrderSide.BUY if side_str == "BUY" else OrderSide.SELL
        sdk_order_type = OrderType.MARKET
        sdk_tif = OrderTIF.DAY

        try:
            res = await self._sdk_call(
                self._trade().order.place_order,
                account_id, qty_int, instrument_id, sdk_side, order_id,
                sdk_order_type, False, sdk_tif,
            )
            data = res.json() if hasattr(res, "json") else res
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"Webull submit_market_order failed: {e}") from e

        # Surface SDK envelope-level errors. Webull's response wraps
        # the result in {code, msg, data}; code "200" means accepted.
        if isinstance(data, dict):
            code = data.get("code")
            if code not in (None, "200", 200):
                raise RuntimeError(
                    f"Webull place_order returned code={code} "
                    f"msg={data.get('msg')!r}"
                )

        body = (data or {}).get("data") if isinstance(data, dict) else None
        body = body if isinstance(body, dict) else (data if isinstance(data, dict) else {})
        return {
            "order_id": str(
                body.get("orderId") or body.get("order_id") or
                body.get("clientOrderId") or order_id
            ),
            "client_order_id": order_id,
            "symbol": symbol.upper(),
            "qty": float(qty_int),
            "notional": float(notional) if notional is not None else float(qty_int) * last_price,
            "side": side_str,
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
        # 2026-02-19: real SDK exposes `query_order_detail(account_id,
        # client_order_id)`, not `get_order_detail`. Same response
        # envelope as the legacy name.
        account_id = await self._resolve_account_id()
        try:
            res = await self._sdk_call(
                self._trade().order.query_order_detail, account_id, order_id,
            )
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
        # 2026-02-19: real SDK exposes `list_open_orders(account_id)`
        # (the account_id is required, not optional). Previously the
        # adapter called `get_order_history(account_id)` which doesn't
        # exist on the v1 surface — fall back to `list_today_orders`
        # for the broadest set of currently-tracked orders, then
        # filter to OPEN-status entries.
        try:
            account_id = await self._resolve_account_id()
            res = await self._sdk_call(
                self._trade().order.list_today_orders, account_id,
            )
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
        # 2026-02-19: real SDK signature is
        # `cancel_order(account_id, client_order_id)` (positional),
        # not a dict payload.
        account_id = await self._resolve_account_id()
        try:
            await self._sdk_call(
                self._trade().order.cancel_order, account_id, order_id,
            )
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"Webull cancel_order failed: {e}") from e

    async def list_positions(self) -> list[BrokerPosition]:
        # 2026-02-19: real SDK does not have a `position.get_positions`
        # surface; positions are read via
        # `account_v2.get_account_position_details(account_id)`.
        try:
            account_id = await self._resolve_account_id()
            res = await self._sdk_call(
                self._trade().account_v2.get_account_position_details, account_id,
            )
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

# 2026-02-19 — Process-wide singleton. Constructing a fresh `ApiClient`
# on every order put the Webull SDK into a token-refresh spin (the
# SDK's `_check_token_enable` cache is per-ApiClient; a brand-new
# instance has nothing cached, hits a hot loop in
# `client_initializer`, burns the executor thread for 25+ seconds,
# and the request comes back as a Cloudflare 502). The quotes-side
# code (`market_data/webull_quotes.py`) singletons its ApiClient for
# the same reason; this mirrors that pattern.
_ADAPTER: Optional[WebullAdapter] = None
_ADAPTER_LOCK = threading.Lock()


def reset_webull_adapter_for_tests() -> None:
    """Tests rebind the singleton. Production never calls this."""
    global _ADAPTER
    with _ADAPTER_LOCK:
        _ADAPTER = None


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

    2026-02-19: process-wide singleton (see module-level note). The
    same ApiClient is reused across every order so the SDK's token
    cache stays warm. If creds change, restart the pod or call
    `reset_webull_adapter_for_tests()` from a test.
    """
    global _ADAPTER
    if not is_webull_armed():
        # When ARMED flips off mid-session we DO NOT reuse a cached
        # client — return None so the route log shows "not configured".
        return None
    if _ADAPTER is not None:
        return _ADAPTER

    app_key = (os.environ.get("WEBULL_APP_KEY") or "").strip()
    app_secret = (os.environ.get("WEBULL_APP_SECRET") or "").strip()
    if not app_key or not app_secret:
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

    with _ADAPTER_LOCK:
        # Double-checked locking — another coroutine may have built it
        # while we were waiting on the lock.
        if _ADAPTER is not None:
            return _ADAPTER
        try:
            api_client = ApiClient(app_key, app_secret, region_id)
            if environment == "uat":
                api_client.add_endpoint(region_id, "us-openapi-alb.uat.webullbroker.com")
        except Exception as e:  # noqa: BLE001
            logger.warning("Webull ApiClient construction failed: %s", e)
            return None
        _ADAPTER = WebullAdapter(api_client=api_client)
        logger.info(
            "Webull adapter singleton initialized region=%s environment=%s",
            region_id, environment,
        )
        return _ADAPTER
