"""Bid/ask spread poller — Kraken (crypto) + Webull (equity).

Doctrine pin (2026-07-02):
    Pull public quote endpoints on bounded loops. Compute bid/ask
    spread in basis points. Cache newest reading per symbol in
    memory for the risk gate; persist a rolling window to the local
    trader store (JSONL + SQLite) for the operator dashboard.

    NEVER touches Mongo. NEVER submits an order. Public endpoints
    only — Kraken /public/Ticker for crypto, Webull's own public
    quote gateway (`quotes-gw.webullbroker.com`) for equity. The
    Webull gateway is the same one the Webull website itself hits,
    unauthenticated, and returns bid/ask directly — no OpenAPI
    credentials required. Yahoo's /v7/finance/quote was rejected
    (2026-07-02) after operator flagged persistent 401/429/empty
    responses in preview and prod.

Two independent pollers run side by side (one per lane) so a Webull
gateway hiccup can't stall the Kraken poller and vice versa. Both
write into a single `spread_ticks` SQLite table with a `source`
column (`kraken` / `webull`) and a shared in-memory cache keyed by
SYMBOL.

Observability-first. Promote to hard risk gates with:
    TRADER_SPREAD_GATE_ENABLED=true          (crypto)
    TRADER_EQUITY_SPREAD_GATE_ENABLED=true   (equity)
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

import httpx

from trader import config, store


logger = logging.getLogger("trader.spread")

KRAKEN_TICKER = "https://api.kraken.com/0/public/Ticker"
# Webull's own public quote gateway — unauthenticated, same endpoint
# the Webull website hits. Two-step: symbol → tickerId, then quote.
WEBULL_SEARCH = "https://quotes-gw.webullbroker.com/api/search/pc/tickers"
WEBULL_QUOTE = "https://quotes-gw.webullbroker.com/api/quote/tickerRealTimes/v5"

# The gateway is picky about headers — a bare request returns 417
# ("Expectation failed"). These headers mirror what the Webull web
# app sends and unblock the endpoint from server side (verified
# 2026-07-02 in preview).
WEBULL_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://www.webull.com",
    "Referer": "https://www.webull.com/",
    "App": "global",
    "App-Group": "broker",
    "Device-Type": "Web",
    "Locale": "eng",
    "OS": "web",
    "Platform": "web",
}

# Cache resolved tickerIds so we don't re-search every poll cycle.
_webull_id_cache: dict[str, int] = {}

# In-memory cache: {symbol.upper() -> {bid, ask, last, spread_bps,
# source, lane, ts_unix}}. The risk gate reads from here — never
# blocks on I/O.
_latest: dict[str, dict] = {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_unix() -> float:
    return datetime.now(timezone.utc).timestamp()


def _row_from_quote(*, symbol: str, lane: str, source: str,
                    bid: float, ask: float,
                    last: Optional[float]) -> Optional[dict]:
    """Compose the canonical spread row from a validated bid/ask."""
    if ask <= 0 or bid <= 0 or ask < bid:
        return None
    mid = (ask + bid) / 2.0
    spread_abs = ask - bid
    spread_bps = (spread_abs / mid) * 10_000 if mid > 0 else 0.0
    return {
        "ts": _now_iso(),
        "pair": symbol.upper(),   # column name is `pair` for both lanes
        "lane": lane,
        "bid": bid, "ask": ask, "last": last,
        "spread_abs": spread_abs,
        "spread_bps": round(spread_bps, 4),
        "source": source,
    }


# ─── Kraken (crypto) ──────────────────────────────────────────────

def _parse_kraken(pair_req: str, result: dict) -> Optional[dict]:
    """Kraken canonicalizes pair names (XBTUSD → XXBTZUSD). Find the
    single non-`last` key in `result`."""
    if not result:
        return None
    bars_key = next((k for k in result if k != "last"), None)
    if not bars_key:
        return None
    row = result.get(bars_key) or {}
    # a = [ask, whole_lot_vol, lot_vol]
    # b = [bid, whole_lot_vol, lot_vol]
    # c = [last_trade_price, last_trade_vol]
    try:
        ask = float((row.get("a") or [0])[0])
        bid = float((row.get("b") or [0])[0])
        last = float((row.get("c") or [0])[0]) if row.get("c") else None
    except (TypeError, ValueError, IndexError):
        return None
    return _row_from_quote(
        symbol=pair_req, lane="crypto", source="kraken",
        bid=bid, ask=ask, last=last,
    )


async def fetch_kraken(client: httpx.AsyncClient, pair: str) -> Optional[dict]:
    try:
        r = await client.get(KRAKEN_TICKER, params={"pair": pair})
        r.raise_for_status()
        j = r.json()
    except Exception as e:  # noqa: BLE001
        logger.warning("spread kraken fetch failed pair=%s err=%s", pair, e)
        return None
    if j.get("error"):
        logger.warning("spread kraken error pair=%s err=%s", pair, j["error"])
        return None
    row = _parse_kraken(pair, j.get("result") or {})
    if not row:
        logger.warning("spread kraken parse failed pair=%s", pair)
    return row


# ─── Webull public quote gateway (equity) ─────────────────────────

async def _resolve_webull_id(client: httpx.AsyncClient,
                             ticker: str) -> Optional[int]:
    """Symbol → Webull internal tickerId. Cached per-process because
    tickerIds don't change for a given symbol."""
    t = ticker.upper()
    cached = _webull_id_cache.get(t)
    if cached:
        return cached
    try:
        r = await client.get(
            WEBULL_SEARCH,
            params={"keyword": t, "pageIndex": 1, "pageSize": 20},
            headers=WEBULL_HEADERS,
        )
        r.raise_for_status()
        j = r.json()
    except Exception as e:  # noqa: BLE001
        logger.warning("spread webull search failed ticker=%s err=%s", t, e)
        return None
    items = (j or {}).get("data") or []
    # Prefer an exact-symbol match; Webull returns symbols in `disSymbol`.
    for it in items:
        sym = (it.get("disSymbol") or it.get("symbol") or "").upper()
        if sym == t and it.get("tickerId"):
            try:
                tid = int(it["tickerId"])
            except (TypeError, ValueError):
                continue
            _webull_id_cache[t] = tid
            return tid
    logger.warning("spread webull no tickerId for ticker=%s (results=%d)",
                   t, len(items))
    return None


def _parse_webull(ticker_req: str, entry: dict) -> Optional[dict]:
    """Webull /tickerRealTimes response — extract bid[0].price and
    ask[0].price. `entry` is the top-level dict (Webull returns a
    single object, not an array, when queried by tickerId)."""
    if not entry:
        return None
    bid = 0.0
    ask = 0.0
    try:
        blist = entry.get("bidList") or []
        alist = entry.get("askList") or []
        if blist:
            bid = float(blist[0].get("price") or 0)
        if alist:
            ask = float(alist[0].get("price") or 0)
    except (TypeError, ValueError, IndexError):
        return None
    # Fallback to flat fields (some symbols surface `bid`/`ask` at root).
    if bid <= 0:
        try:
            bid = float(entry.get("pPrice") or entry.get("bid") or 0)
        except (TypeError, ValueError):
            bid = 0.0
    if ask <= 0:
        try:
            ask = float(entry.get("ask") or 0)
        except (TypeError, ValueError):
            ask = 0.0
    last = None
    for k in ("close", "pPrice", "price"):
        v = entry.get(k)
        if v is not None:
            try:
                last = float(v)
                break
            except (TypeError, ValueError):
                continue
    return _row_from_quote(
        symbol=ticker_req, lane="equity", source="webull",
        bid=bid, ask=ask, last=last,
    )


async def fetch_webull(client: httpx.AsyncClient,
                       ticker: str) -> Optional[dict]:
    tid = await _resolve_webull_id(client, ticker)
    if not tid:
        return None
    try:
        r = await client.get(f"{WEBULL_QUOTE}/{tid}", headers=WEBULL_HEADERS)
        r.raise_for_status()
        j = r.json()
    except Exception as e:  # noqa: BLE001
        logger.warning("spread webull quote fetch failed ticker=%s tid=%s err=%s",
                       ticker, tid, e)
        return None
    # Webull returns either a dict OR a list (varies per endpoint version).
    entry = j[0] if isinstance(j, list) and j else j if isinstance(j, dict) else None
    row = _parse_webull(ticker, entry or {})
    if not row:
        logger.warning("spread webull parse failed ticker=%s (no bid/ask)", ticker)
    return row


# ─── in-memory cache helpers ──────────────────────────────────────

def _cache_row(row: dict) -> None:
    _latest[row["pair"].upper()] = {**row, "ts_unix": _now_unix()}


def latest(symbol: Optional[str] = None) -> dict | list[dict]:
    """Return the newest reading for `symbol`, or all known symbols
    if omitted. Includes `ts_unix` for local staleness math."""
    if symbol:
        return dict(_latest.get(symbol.upper()) or {})
    return [dict(v) for v in _latest.values()]


def is_stale(symbol: str, max_age_sec: Optional[int] = None) -> bool:
    row = _latest.get((symbol or "").upper())
    if not row:
        return True
    age = _now_unix() - float(row.get("ts_unix") or 0)
    limit = max_age_sec if max_age_sec is not None else config.spread_stale_sec()
    return age > limit


def _cap_and_gate_for_lane(lane: str) -> tuple[bool, float]:
    """Return (gate_enabled, max_bps) for the given lane."""
    if (lane or "").lower() == "equity":
        return config.equity_spread_gate_enabled(), config.equity_spread_max_bps()
    return config.spread_gate_enabled(), config.spread_max_bps()


def check_spread_ok(symbol: str,
                    lane: str = "crypto") -> tuple[bool, str, Optional[float]]:
    """Risk-gate helper. Returns (ok, reason, observed_bps).

    Contract:
      * Gate disabled     → (True, "gate_disabled", <bps_or_None>)
      * No/stale reading  → (True, "spread_stale",  None)   [fail-open]
      * Wide              → (False, "spread_wide:<bps>>cap", bps)
      * Otherwise         → (True, "spread_ok",     bps)
    """
    row = _latest.get((symbol or "").upper()) or {}
    bps = row.get("spread_bps")
    gate_on, cap = _cap_and_gate_for_lane(lane)
    if not gate_on:
        return True, "gate_disabled", bps
    if is_stale(symbol):
        return True, "spread_stale", None
    if bps is not None and bps > cap:
        return False, f"spread_wide:{bps:.2f}bps>{cap:.2f}bps", bps
    return True, "spread_ok", bps


# ─── per-lane polling loops ───────────────────────────────────────

async def _record_and_cache(row: Optional[dict]) -> None:
    if not row:
        return
    _cache_row(row)
    try:
        store.record_spread_tick(row)
    except Exception as e:  # noqa: BLE001
        logger.error("spread store write failed pair=%s err=%s",
                     row.get("pair"), e)


async def poll_kraken_once(client: httpx.AsyncClient) -> list[dict]:
    out: list[dict] = []
    for pair in config.spread_pairs():
        row = await fetch_kraken(client, pair)
        await _record_and_cache(row)
        if row:
            out.append(row)
    return out


async def poll_webull_once(client: httpx.AsyncClient) -> list[dict]:
    out: list[dict] = []
    for tkr in config.equity_spread_tickers():
        row = await fetch_webull(client, tkr)
        await _record_and_cache(row)
        if row:
            out.append(row)
    return out


async def _kraken_loop() -> None:
    if not config.spread_enabled():
        logger.info("spread kraken poller DISABLED")
        return
    interval = max(5, config.spread_poll_sec())
    logger.info(
        "spread kraken poller STARTED pairs=%s interval=%ss gate=%s max_bps=%.1f",
        list(config.spread_pairs()), interval,
        config.spread_gate_enabled(), config.spread_max_bps(),
    )
    async with httpx.AsyncClient(timeout=8.0) as client:
        while True:
            try:
                await asyncio.wait_for(poll_kraken_once(client), timeout=30)
            except asyncio.CancelledError:
                logger.info("spread kraken poller cancelled")
                raise
            except Exception as e:  # noqa: BLE001
                logger.warning("spread kraken cycle error: %s", e)
            await asyncio.sleep(interval)


async def _webull_loop() -> None:
    if not config.equity_spread_enabled():
        logger.info("spread webull poller DISABLED")
        return
    interval = max(5, config.equity_spread_poll_sec())
    logger.info(
        "spread webull poller STARTED tickers=%s interval=%ss gate=%s max_bps=%.1f",
        list(config.equity_spread_tickers()), interval,
        config.equity_spread_gate_enabled(),
        config.equity_spread_max_bps(),
    )
    async with httpx.AsyncClient(timeout=8.0) as client:
        while True:
            try:
                await asyncio.wait_for(poll_webull_once(client), timeout=30)
            except asyncio.CancelledError:
                logger.info("spread webull poller cancelled")
                raise
            except Exception as e:  # noqa: BLE001
                logger.warning("spread webull cycle error: %s", e)
            await asyncio.sleep(interval)


async def poll_loop() -> None:
    """Fan-out entrypoint: runs Kraken + Webull pollers concurrently.
    Cancelling this task cancels both children."""
    tasks = [
        asyncio.create_task(_kraken_loop(), name="trader.spread.kraken"),
        asyncio.create_task(_webull_loop(), name="trader.spread.webull"),
    ]
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        for t in tasks:
            t.cancel()
        raise
