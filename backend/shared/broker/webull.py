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
        # 2026-02-19 (later): the real Webull `get_account_balance`
        # response is nested + snake_case, not the camelCase top-level
        # shape the prior parser assumed. Actual surface:
        #
        #   {
        #     "total_net_liquidation_value": "677.67",
        #     "total_cash_balance":          "676.68",
        #     "account_currency_assets": [{
        #       "currency":            "USD",
        #       "cash_balance":        "676.68",
        #       "settled_cash":        "676.68",
        #       "buying_power":        "0.00",
        #       "option_buying_power": "676.68",
        #       "net_liquidation_value": "677.67",
        #       ...
        #     }]
        #   }
        #
        # Two gotchas the previous parser missed:
        #   1. Per-currency detail is nested under
        #      `account_currency_assets[<USD>]`, not the top level.
        #   2. For INDIVIDUAL_CASH sub-accounts, the literal
        #      `buying_power` field is *0.00* — cash accounts don't
        #      use "buying power", they spend `settled_cash` directly.
        #      MC must coalesce `buying_power` → `settled_cash` →
        #      `cash_balance` so the gate chain sees real headroom.
        #
        # Old field names (cashBalance / buyingPower / netLiquidation)
        # are kept as fallbacks in case Webull adds a camelCase alias
        # or a future SDK version normalizes the shape — but the
        # snake_case + nested path is the one that actually works
        # against today's prod endpoint.
        account_id = await self._resolve_account_id()
        try:
            res = await self._sdk_call(
                self._trade().account_v2.get_account_balance, account_id,
            )
            data = res.json() if hasattr(res, "json") else res
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"Webull get_account failed: {e}") from e

        # Tolerate the legacy `{"data": {...}}` envelope just in case
        # a future SDK build adds one back.
        d = (data or {}).get("data") if isinstance(data, dict) and "data" in data else data
        d = d or {}

        # Per-currency entry — prefer USD; fall back to whatever's at
        # [0] if USD isn't listed (e.g., crypto-only sub-account).
        cur_assets = d.get("account_currency_assets") or []
        cur: dict = {}
        if isinstance(cur_assets, list) and cur_assets:
            usd = next(
                (a for a in cur_assets if (a or {}).get("currency", "").upper() == "USD"),
                cur_assets[0],
            )
            cur = usd or {}

        def _f(*keys: str) -> float:
            """Pick the first numeric value found across keys, in order."""
            for k in keys:
                v = cur.get(k) if k in cur else d.get(k)
                if v is None:
                    continue
                try:
                    return float(v)
                except (TypeError, ValueError):
                    continue
            return 0.0

        cash = _f(
            "settled_cash",          # cash account — actually spendable
            "cash_balance",          # snake_case nested
            "total_cash_balance",    # snake_case top-level total
            "cashBalance",           # legacy camelCase fallback
            "cash",                  # legacy short-form fallback
        )
        # For cash accounts the broker's `buying_power` is 0 by design.
        # Coalesce up through settled_cash so the cap gate sees real
        # purchasing power on Individual Cash subs.
        bp_raw = _f("buying_power", "buyingPower", "dayBuyingPower")
        bp = bp_raw if bp_raw > 0 else cash

        equity = _f(
            "net_liquidation_value",       # snake_case nested
            "total_net_liquidation_value", # snake_case top-level total
            "netLiquidation",              # legacy camelCase fallback
            "totalAssetValue",             # very old fallback
        ) or bp

        return {
            "account_number": account_id,
            "status": d.get("status", "ACTIVE"),
            "equity": equity,
            "cash": cash,
            "buying_power": bp,
            "daytrade_buying_power": bp,
            "last_equity": equity,
            "pattern_day_trader": bool(
                cur.get("pattern_day_trader")
                or d.get("pattern_day_trader")
                or d.get("patternDayTrader")
                or False
            ),
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

    @staticmethod
    def _extended_hours_branch(
        *,
        lane: str,
        last_price: float,
        side: str,
    ) -> tuple[str, Optional[str], str, bool]:
        """Return `(order_type, limit_price_str, session, ext_hours_flag)`
        for the Webull v2 stock_order body.

        Doctrine (2026-02-26 — operator-pinned, supersedes 2026-06-22):
          * Crypto lanes (non-equity) → MARKET, CORE session.
          * Equity lanes → **LIMIT regardless of session**. Webull's
            `entrust_type=AMOUNT` (the only path that ships a cash
            amount to convert to fractional shares) is INCOMPATIBLE
            with `order_type=MARKET`: the broker returns
            HTTP 417 / INVALID_PARAMETER "The time you sent is not
            supported" — a misleading message that actually refers to
            the order_type/entrust_type/time_in_force combination,
            not the signing timestamp. (Operator hit this 8+ times in
            preview on 2026-06-29 with a real-wall-clock-correct
            container; verified via direct-execute autopsy.)

            We always compute a LIMIT price from `last_price` with a
            buy/sell-adjusted slippage band so the order still fills
            against the inside book during BOTH RTH and pre/post.
            During RTH the `support_trading_session` stays `CORE`;
            during extended hours we flip to the env-configurable
            session ("ALL" by default) and set `extended_hours_trading=True`.

        Tunable via env (no redeploy):
          * `WEBULL_LIMIT_SLIPPAGE_BPS` (default 50 = 0.5%) — the RTH
            slippage band on last_price for the LIMIT.
          * `WEBULL_EXTENDED_HOURS_SLIPPAGE_BPS` (default 100 = 1.0%) —
            wider slippage band during the thinner pre/post session.
          * `WEBULL_EXTENDED_HOURS_SESSION` (default "ALL") — Webull
            session enum for extended-hours-eligible orders.

        Returns the tuple in the exact shape `submit_market_order`
        needs to assemble the v2 stock_order body.
        """
        # Non-equity lanes (crypto) never apply RTH semantics —
        # Webull crypto trades 24/7 against MARKET orders.
        if lane != "equity":
            return "MARKET", None, "CORE", False

        # Decide RTH vs extended (controls session enum + slippage band
        # width). Both branches return LIMIT — see doctrine above.
        try:
            from shared.market_hours import is_equity_rth  # noqa: WPS433
            in_rth = is_equity_rth()
        except Exception:  # noqa: BLE001
            # Defensive: if the helper raises (it shouldn't), assume
            # RTH so we use the tighter slippage band.
            in_rth = True

        if last_price <= 0:
            # No reference price → we cannot compose a LIMIT. Fall
            # back to MARKET; the broker's reject (417) becomes the
            # observable error so the operator can see "no last_price".
            return "MARKET", None, "CORE", False

        if in_rth:
            try:
                slippage_bps = float(
                    os.environ.get("WEBULL_LIMIT_SLIPPAGE_BPS", "50")
                )
            except (TypeError, ValueError):
                slippage_bps = 50.0
            session = "CORE"
            ext_flag = False
        else:
            try:
                slippage_bps = float(
                    os.environ.get("WEBULL_EXTENDED_HOURS_SLIPPAGE_BPS", "100")
                )
            except (TypeError, ValueError):
                slippage_bps = 100.0
            session = (
                os.environ.get("WEBULL_EXTENDED_HOURS_SESSION") or "ALL"
            ).upper().strip()
            ext_flag = True

        slippage_bps = max(0.0, slippage_bps)
        adj = slippage_bps / 10_000.0  # bps → fraction
        if side == "BUY":
            limit_price = last_price * (1.0 + adj)
        else:  # SELL
            limit_price = last_price * (1.0 - adj)
        # Webull documents US-equity prices at 2 decimals.
        return "LIMIT", f"{limit_price:.2f}", session, ext_flag

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

        2026-02-19 (rev 3) — FRACTIONAL SHARES via place_order_v2:
        Operator confirmed they bought $1 of NVDA via Webull's own
        UI today. Webull's standard `place_order` (v1) is documented
        for HK / China-Connect markets and accepts integer qty only —
        which is why the previous adapter floor-divided notional and
        bailed with "WEBULL_QTY_BELOW_ONE" on every ticker priced
        above the $10 cap. The real US fractional path is:

            order.place_order_v2(account_id, stock_order_dict)

        where `stock_order_dict` carries `entrust_type="AMOUNT"` and
        `total_cash_amount="<dollars>"`, e.g.,

            {
              "client_order_id": "...",
              "symbol": "NVDA",
              "instrument_id": "<resolved>",
              "instrument_type": "EQUITY",
              "market": "US",
              "order_type": "MARKET",
              "side": "BUY",
              "time_in_force": "DAY",
              "entrust_type": "AMOUNT",
              "total_cash_amount": "1.00",
              "support_trading_session": "CORE",
              "account_tax_type": "GENERAL"
            }

        AMOUNT mode is intended for sub-share fractional buys — which
        is exactly our $1-$10 pilot band. With NVDA at ~$140 a $10
        intent buys ~0.07 shares; the broker handles the rounding.

        Path selection:
          * `notional` provided → v2 + AMOUNT (fractional). The whole
            pilot lives here.
          * `qty` provided      → v1 + integer (legacy; reconcile /
            manual whole-share paths only). Untouched.
        """
        # ─── 2026-02-20: pre-submit RTH session guard ─────────────
        # The Webull 417 "The time you sent is not supported" error
        # was killing ~337 submissions / 72h. Root cause: MARKET
        # orders submitted within the last 60s of the regular session
        # (or outside RTH entirely) are rejected by the broker because
        # they can't be filled at-market in the remaining window.
        # `support_trading_session: "CORE"` requires the ORDER to
        # arrive INSIDE regular hours, and Webull enforces that with
        # a wall-clock check.
        #
        # Doctrine pin (operator 2026-02-20):
        #     "If outside supported session, RoadGuard should block
        #      before broker submit."
        #
        # We block here (in-adapter) as belt-and-braces — RoadGuard's
        # session check is upstream, but the adapter is the LAST stop
        # before the HTTP call so the guard prevents broker round-
        # trips that we know will 417. Receipt surfaces in the
        # post-mortem as `skip_category: outside_rth_for_market`,
        # which the operator can act on cleanly.
        lane = _lane_for_symbol(symbol)
        # 2026-02-20: the RTH/close-buffer guard previously lived
        # here in the adapter — moved upstream into
        # `shared/pipeline/roadguard.py::_within_webull_core_close_buffer`
        # so the post-mortem sees a clean `roadguard_blocked` verdict
        # ("WEBULL_CORE_MARKET_ORDER_CLOSE_BUFFER") instead of a broker
        # `submit_raised` HTTP 417 surprise. Per operator pin:
        # "That should become a RoadGuard pre-submit block, not a
        # Webull adapter surprise."

        # Belt-and-braces re-check of the cap. 2026-02-20: the cap is
        # now buying-power-scaled, so we fetch the adapter's own
        # account balance here so the re-check uses the same dynamic
        # ceiling the router used. Falls back to env-only on any
        # fetch error — never silently approves.
        bp_usd: Optional[float] = None
        try:
            acct = await self.get_account()
            bp_raw = float(acct.get("buying_power") or 0.0)
            bp_usd = bp_raw if bp_raw > 0 else None
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "webull adapter pre-trade BP fetch failed (falling back "
                "to env cap): %s", e,
            )
        decision = evaluate_webull_order(
            notional_usd=notional,
            symbol=symbol,
            buying_power_usd=bp_usd,
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

        if lane is None:
            raise RuntimeError(
                f"Webull adapter: no lane known for symbol {symbol!r}; NO_TRADE"
            )

        # Resolve the instrument_id + last price (cached).
        instrument_id, last_price, fractionable = await self._resolve_instrument_id(symbol)

        sym_u = (symbol or "").upper().strip()

        if notional is not None:
            # ── FRACTIONAL PATH (v2 + QTY) ────────────────────────────
            # Doctrine pin (2026-02-26): Webull's v2 API DEPRECATED the
            # AMOUNT entrust_type. The only accepted value per the
            # current docs is `QTY` with `quantity` as a string
            # (decimal supported for fractional US equities).
            # Sending `entrust_type=AMOUNT` + `total_cash_amount` causes
            # Webull to reject with HTTP 417 / INVALID_PARAMETER and
            # the misleading message "The time you sent is not
            # supported." That was the actual root cause of the
            # operator-reported "broker submit errors" — verified
            # against `/api/admin/direct-execute/recent` payload dump
            # against developer.webull.com/apis/docs.
            #
            # We convert notional → fractional qty using `last_price`,
            # serialize with 6-decimal precision (well below Webull's
            # documented decimal allowance for fractional US stocks),
            # and use LIMIT + slippage band so the cash spend stays
            # within the operator's per-order cap even if the price
            # ticks up between quote and fill.
            effective_notional = float(notional)
            if last_price <= 0:
                raise RuntimeError(
                    f"Webull last-price unavailable for {sym_u}; "
                    f"cannot size fractional QTY order; NO_TRADE"
                )

            order_kind, limit_price_str, session_str, ext_flag = (
                self._extended_hours_branch(
                    lane=lane,
                    last_price=last_price,
                    side=side_str,
                )
            )
            # Compute qty against the LIMIT price (if LIMIT) so the
            # cash spend never exceeds `effective_notional` even at
            # worst-case fill. For MARKET (whole-share fallback) we
            # use last_price.
            price_for_qty = (
                float(limit_price_str) if limit_price_str else last_price
            )
            raw_qty = effective_notional / price_for_qty if price_for_qty > 0 else 0.0
            # 6-dp truncate (not round-half-up) so the resulting cash
            # spend is always ≤ effective_notional.
            qty_truncated = int(raw_qty * 1_000_000) / 1_000_000.0
            if qty_truncated <= 0:
                raise RuntimeError(
                    f"Webull QTY sizing produced zero for {sym_u} "
                    f"(notional={effective_notional} price={price_for_qty}); "
                    f"NO_TRADE"
                )
            qty_str = f"{qty_truncated:.6f}".rstrip("0").rstrip(".")
            if "." not in qty_str:
                qty_str = f"{qty_str}.0"

            stock_order = {
                "client_order_id": order_id,
                "symbol": sym_u,
                "instrument_type": "EQUITY",
                "market": "US",
                "order_type": order_kind,
                "side": side_str,
                "time_in_force": "DAY",
                "entrust_type": "QTY",
                "quantity": qty_str,
                "support_trading_session": session_str,
                "account_tax_type": "GENERAL",
            }
            if order_kind == "LIMIT" and limit_price_str is not None:
                stock_order["limit_price"] = limit_price_str

            # Log MC receipt provenance (signature prefix only).
            if mc_receipt:
                sig = (mc_receipt.get("signature") or "")[:12]
                logger.info(
                    "Webull submit_market_order (v2/QTY-frac) receipt_sig=%s "
                    "symbol=%s instrument_id=%s lane=%s side=%s "
                    "quantity=%s notional=%.2f last_price=%s",
                    sig, sym_u, instrument_id, lane, side_str,
                    qty_str, effective_notional, last_price,
                )
            else:
                logger.info(
                    "Webull submit_market_order (v2/QTY-frac) symbol=%s "
                    "instrument_id=%s lane=%s side=%s quantity=%s "
                    "notional=%.2f last_price=%s",
                    sym_u, instrument_id, lane, side_str, qty_str,
                    effective_notional, last_price,
                )

            try:
                logger.info(
                    "Webull v2 REQUEST account_id=%s stock_order=%r",
                    account_id, stock_order,
                )
                res = await self._sdk_call(
                    self._trade().order.place_order_v2,
                    account_id, stock_order,
                )
                data = res.json() if hasattr(res, "json") else res
            except Exception as e:  # noqa: BLE001
                raise RuntimeError(f"Webull submit_market_order (v2) failed: {e}") from e

            # Surface SDK envelope-level errors. Webull's response
            # wraps the result in {code, msg, data}; code "200" means
            # accepted.
            if isinstance(data, dict):
                code = data.get("code")
                if code not in (None, "200", 200):
                    raise RuntimeError(
                        f"Webull place_order_v2 returned code={code} "
                        f"msg={data.get('msg')!r}"
                    )

            body = (data or {}).get("data") if isinstance(data, dict) else None
            body = body if isinstance(body, dict) else (data if isinstance(data, dict) else {})

            # Fractional qty estimate for receipt — broker will fill
            # the exact decimal share count, but we surface our best
            # estimate so the receipts dashboard shows non-zero qty
            # before the fill report arrives.
            est_qty = (effective_notional / last_price) if last_price > 0 else 0.0

            return {
                "order_id": str(
                    body.get("orderId") or body.get("order_id") or
                    body.get("clientOrderId") or order_id
                ),
                "client_order_id": order_id,
                "symbol": sym_u,
                "qty": float(body.get("filledQuantity") or est_qty),
                "notional": effective_notional,
                "side": side_str,
                "type": order_kind.lower(),
                "limit_price": (
                    float(limit_price_str) if limit_price_str else None
                ),
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

        # ── WHOLE-SHARE PATH (v1 + integer qty) ──────────────────────
        # Only used by callers that pass `qty` explicitly (reconcile,
        # manual operator scripts). The auto-router never hits this
        # branch — it always sends notional intents through the v2
        # fractional path above.
        qty_int = int(float(qty))
        if qty_int < 1:
            raise WebullCapBlocked(
                f"WEBULL_QTY_BELOW_ONE — qty={qty} < 1; the integer "
                f"whole-share path requires qty >= 1. Pass `notional` "
                f"instead for fractional via v2/AMOUNT."
            )

        # Log MC receipt provenance (signature prefix only).
        if mc_receipt:
            sig = (mc_receipt.get("signature") or "")[:12]
            logger.info(
                "Webull submit_market_order (v1/QTY) receipt_sig=%s "
                "symbol=%s instrument_id=%s lane=%s side=%s qty=%s "
                "last_price=%s",
                sig, sym_u, instrument_id, lane, side_str, qty_int,
                last_price,
            )
        else:
            logger.info(
                "Webull submit_market_order (v1/QTY) symbol=%s "
                "instrument_id=%s lane=%s side=%s qty=%s last_price=%s",
                sym_u, instrument_id, lane, side_str, qty_int, last_price,
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
            "symbol": sym_u,
            "qty": float(qty_int),
            "notional": float(qty_int) * last_price,
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

    async def submit_otoco_market(
        self,
        symbol: str,
        qty: int,
        side: str,
        target_price: float,
        stop_price: float,
        *,
        client_order_id: Optional[str] = None,
        mc_receipt: Optional[dict] = None,
    ) -> BrokerOrder:
        """Atomic OTOCO bracket — MARKET entry + LIMIT take-profit +
        STOP stop-loss, submitted to Webull as a single combo so the
        broker manages the lifecycle (TP fill cancels SL automatically
        and vice-versa).

        Doctrine (Phase 2, 2026-02-19):

          * Webull's combo API uses v3 `order_v3.place_order` with
            three `new_orders` and a `client_combo_order_id`.
          * `combo_type=MASTER` is the entry leg; `combo_type=OTOCO`
            on each child marks them as the TP/SL pair.
          * INTEGER share quantity ONLY. Combo orders do NOT support
            the `entrust_type=AMOUNT` fractional path used by the
            $1-$10 small-pilot route. Callers must compute a
            whole-share qty BEFORE invoking this method. Fractional
            intents stay on the existing `submit_market_order` +
            passive bracket-recorder pathway.
          * Side semantics: BUY entry → SELL legs on TP/SL; SELL/SHORT
            entry → BUY legs on TP/SL (cover). The adapter computes
            the child side; callers only specify the entry side.
          * Doctrine sanity check on the bracket: for BUY entry,
            `stop_price < entry < target_price`; for SELL, the
            inverse. Last-trade price is used as the entry proxy
            because the master leg is MARKET (the actual fill price
            may drift; the brain's thesis is still the right
            reference for sanity).

        Args:
            symbol: ticker / canonical pair (resolved via the same
                instrument cache the market path uses).
            qty: integer number of shares for the entry AND each
                child leg. Must be >= 1.
            side: "BUY" or "SELL" for the entry leg.
            target_price: TP limit price for the OCO child.
            stop_price: SL stop price for the OCO child.
            client_order_id: optional MC-side ID; if omitted the
                adapter mints a UUID. Webull caps client IDs at 40
                chars; we use the same truncation rule as the market
                path.
            mc_receipt: MC's signed execution receipt (logged for
                provenance).

        Returns:
            BrokerOrder dict shaped like `submit_market_order` but
            with extra fields:
              * `combo_order_id` — the master leg's broker order id.
              * `tp_client_order_id`, `sl_client_order_id` — child
                leg client IDs (so the resolver/cancel paths can
                target the OCO pair).
              * `combo_client_order_id` — the umbrella ID used as
                `client_combo_order_id` on the request.

        Raises:
            WebullCapBlocked — adapter not armed / above per-ticker
                $1-$10 cap.
            RuntimeError — SDK envelope failure, malformed bracket,
                or qty < 1.
        """
        if not is_webull_armed():
            raise WebullCapBlocked(
                "WEBULL_NOT_ARMED — set WEBULL_ARMED=true in .env; NO_TRADE"
            )
        if qty < 1 or qty != int(qty):
            raise RuntimeError(
                f"OTOCO requires integer qty >= 1, got {qty!r} — "
                "fractional intents must use submit_market_order"
            )
        side_str = _norm_side(side)
        if side_str not in ("BUY", "SELL"):
            raise RuntimeError(
                f"OTOCO entry side must be BUY or SELL, got {side!r}"
            )
        if target_price <= 0 or stop_price <= 0:
            raise RuntimeError(
                f"OTOCO target_price/stop_price must be positive, got "
                f"tp={target_price!r} sl={stop_price!r}"
            )

        # Resolve instrument + last price for the bracket sanity
        # check (entry proxy).
        instrument_id, last_price, _frac = await self._resolve_instrument_id(symbol)

        # Doctrine sanity: ensure the bracket shape is coherent against
        # the entry-proxy price. A malformed bracket (stop above entry
        # on a BUY, etc.) would let the broker fire the wrong leg
        # immediately. Fail closed — never submit a bracket whose
        # geometry contradicts the entry direction.
        if side_str == "BUY":
            if not (stop_price < last_price < target_price):
                raise RuntimeError(
                    f"OTOCO BUY bracket malformed: stop={stop_price:.4f} "
                    f"entry≈{last_price:.4f} target={target_price:.4f} — "
                    f"expected stop < entry < target"
                )
            tp_side = "SELL"
            sl_side = "SELL"
        else:  # SELL
            if not (target_price < last_price < stop_price):
                raise RuntimeError(
                    f"OTOCO SELL bracket malformed: target={target_price:.4f} "
                    f"entry≈{last_price:.4f} stop={stop_price:.4f} — "
                    f"expected target < entry < stop"
                )
            tp_side = "BUY"
            sl_side = "BUY"

        sym_u = (symbol or "").upper().strip()
        lane = _lane_for_symbol(symbol)
        if lane is None:
            raise RuntimeError(
                f"Webull OTOCO: no lane known for symbol {symbol!r}; NO_TRADE"
            )

        account_id = await self._resolve_account_id()
        master_id = (client_order_id or str(uuid.uuid4()))[:40]
        # Per Webull combo docs: combo_id is the umbrella; each leg
        # carries its own unique client_order_id.
        combo_id = f"combo-{master_id[:32]}"[:40]
        tp_id = f"tp-{master_id[:36]}"[:40]
        sl_id = f"sl-{master_id[:36]}"[:40]
        qty_str = str(int(qty))

        # Format prices to 2 decimals (Webull's documented precision
        # for US equities). Brain target/stop already arrive in
        # dollar precision, but be defensive.
        tp_str = f"{float(target_price):.2f}"
        sl_str = f"{float(stop_price):.2f}"

        common = {
            "symbol": sym_u,
            "instrument_id": instrument_id,
            "instrument_type": "EQUITY",
            "market": "US",
            "quantity": qty_str,
            "time_in_force": "DAY",
            "entrust_type": "QTY",
            # 2026-02-20: reverted to CORE — see note on v2/AMOUNT path.
            # Webull MARKET orders are not eligible for extended hours.
            "support_trading_session": "CORE",
            "account_tax_type": "GENERAL",
            "extended_hours_trading": False,
        }
        master_leg = {
            **common,
            "client_order_id": master_id,
            "combo_type": "MASTER",
            "order_type": "MARKET",
            "side": side_str,
        }
        tp_leg = {
            **common,
            "client_order_id": tp_id,
            "combo_type": "OTOCO",
            "order_type": "LIMIT",
            "limit_price": tp_str,
            "side": tp_side,
        }
        sl_leg = {
            **common,
            "client_order_id": sl_id,
            "combo_type": "OTOCO",
            "order_type": "STOP",
            "stop_price": sl_str,
            "side": sl_side,
        }
        new_orders = [master_leg, tp_leg, sl_leg]

        if mc_receipt:
            sig = (mc_receipt.get("signature") or "")[:12]
            logger.info(
                "Webull submit_otoco_market receipt_sig=%s symbol=%s qty=%s "
                "side=%s entry≈%.4f tp=%s sl=%s combo_id=%s",
                sig, sym_u, qty_str, side_str, last_price, tp_str, sl_str,
                combo_id,
            )
        else:
            logger.info(
                "Webull submit_otoco_market symbol=%s qty=%s side=%s "
                "entry≈%.4f tp=%s sl=%s combo_id=%s",
                sym_u, qty_str, side_str, last_price, tp_str, sl_str, combo_id,
            )

        try:
            res = await self._sdk_call(
                self._trade().order_v3.place_order,
                account_id, new_orders, combo_id,
            )
            data = res.json() if hasattr(res, "json") else res
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"Webull submit_otoco_market (v3) failed: {e}") from e

        if isinstance(data, dict):
            code = data.get("code")
            if code not in (None, "200", 200):
                raise RuntimeError(
                    f"Webull order_v3.place_order returned code={code} "
                    f"msg={data.get('msg')!r}"
                )

        body = (data or {}).get("data") if isinstance(data, dict) else None
        body = body if isinstance(body, dict) else (data if isinstance(data, dict) else {})

        # The combo response generally returns the master order id
        # under one of {orderId, master_order_id, parent_order_id};
        # be defensive across surface drift.
        master_broker_id = (
            body.get("orderId")
            or body.get("master_order_id")
            or body.get("parent_order_id")
            or master_id
        )

        now_iso = body.get("createTime") or body.get("submitted_at")

        # Estimate notional = qty * last_price. The actual fill might
        # drift on a MARKET entry but we surface our best estimate so
        # the receipts dashboard has a value before the fill report
        # arrives.
        est_notional = float(qty) * float(last_price) if last_price > 0 else 0.0

        return {
            "order_id": str(master_broker_id),
            "client_order_id": master_id,
            "symbol": sym_u,
            "qty": float(qty),
            "notional": est_notional,
            "side": side_str,
            "type": "otoco_market",
            "status": body.get("status") or "SUBMITTED",
            "submitted_at": now_iso,
            "filled_at": None,
            "filled_qty": 0.0,
            "filled_avg_price": None,
            "combo_order_id": str(master_broker_id),
            "combo_client_order_id": combo_id,
            "tp_client_order_id": tp_id,
            "sl_client_order_id": sl_id,
            "tp_limit_price": float(target_price),
            "sl_stop_price": float(stop_price),
            "entry_proxy_price": float(last_price),
            "lane": lane,
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

    async def list_open_orders_v3(self, page_size: int = 50) -> list[dict]:
        """List open orders via the v3 API (the combo-aware surface).

        Unlike `list_open_orders` (which falls back to the v1
        today-orders endpoint and loses combo metadata), this method
        keeps every field Webull returns — including the combo
        identifiers we need to group an OTOCO bracket's three legs
        back together for the operator dashboard.

        Returns the raw `data` array; the route-level grouper does
        the OTOCO assembly so the adapter stays presentation-free.
        """
        try:
            account_id = await self._resolve_account_id()
            res = await self._sdk_call(
                self._trade().order_v3.get_order_open, account_id, page_size,
            )
            data = res.json() if hasattr(res, "json") else res
        except Exception as e:  # noqa: BLE001
            logger.warning("list_open_orders_v3 failed: %s", e)
            return []
        if isinstance(data, dict):
            code = data.get("code")
            if code not in (None, "200", 200):
                logger.warning(
                    "list_open_orders_v3 SDK envelope code=%s msg=%s",
                    code, data.get("msg"),
                )
                return []
            rows = data.get("data") or []
        else:
            rows = data or []
        return rows if isinstance(rows, list) else []

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
        # ─── Clock-skew compensator (2026-02-26) ─────────────────
        # If the system clock disagrees with real wall time (preview
        # pods set fictional dates), Webull's signing verifier rejects
        # every request with HTTP 417 "The time you sent is not
        # supported." We patch the SDK's two timestamp helpers ONCE,
        # before the first ApiClient is built, so every signed request
        # leaves with a real-wall-clock timestamp. Idempotent. Falls
        # open if the network probe fails.
        try:
            from shared.broker.webull_clock_skew import (  # noqa: WPS433
                install_webull_clock_skew_compensator,
            )
            report = install_webull_clock_skew_compensator()
            logger.info("Webull clock-skew compensator: %s", report)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Webull clock-skew compensator install raised: %s", exc)
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
