"""Bid/ask spread poller — Kraken (crypto) + Webull OpenAPI (equity).

Doctrine pin (2026-07-02):
    Pull L1 quote endpoints on bounded loops. Compute bid/ask spread
    in basis points. Cache newest reading per symbol in memory for
    the risk gate; persist a rolling window to the local trader
    store (JSONL + SQLite) for the operator dashboard.

    NEVER touches Mongo. NEVER submits an order.

    Crypto: Kraken /public/Ticker (unauthenticated).
    Equity: Webull OpenAPI /openapi/market-data/stock/snapshot.
            Requires the L1 market-data subscription active on the
            operator's OpenAPI plan (separate from any in-app sub).
            Auth via `X-Request-App-Key` + `X-Request-App-Secret`,
            same env vars the trade adapter already uses.

Two independent pollers run side by side (one per lane) so a Webull
hiccup can't stall the Kraken poller and vice versa. Both write into
a single `spread_ticks` SQLite table with a `source` column
(`kraken` / `webull`) and a shared in-memory cache keyed by SYMBOL.

Observability-first. Promote to hard risk gates with:
    TRADER_SPREAD_GATE_ENABLED=true          (crypto)
    TRADER_EQUITY_SPREAD_GATE_ENABLED=true   (equity)

Same-broker doctrine (2026-07-02, operator directive):
    Executing on Webull → we source Level 1 quotes from Webull too.
    Eliminates cross-vendor quote drift between the data the brains
    saw and the venue the order lands on. The MQTT stream variant
    (`quotes-stream.webullsolutions.com`) is a natural upgrade path
    once we outgrow the snapshot polling cadence.
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
# Webull OpenAPI market-data snapshot — the ONLY reliable L1 source
# for equities. Requires the L1 subscription to be active on the
# operator's OpenAPI plan (separate from the in-app subscription).
# Auth via `X-Request-App-Key` + `X-Request-App-Secret`.
# Base URL is env-overridable so we can point at UAT
# (`us-openapi-alb.uat.webullbroker.com`) or a regional prod endpoint
# without a code change.
WEBULL_OPENAPI_BASE_DEFAULT = "https://api.webull.com"
WEBULL_SNAPSHOT_PATH = "/openapi/market-data/stock/snapshot"

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


# ─── Webull OpenAPI market-data snapshot (equity) ─────────────────

def _webull_openapi_base() -> str:
    import os
    return os.environ.get("WEBULL_OPENAPI_BASE") or WEBULL_OPENAPI_BASE_DEFAULT


def _webull_openapi_headers() -> Optional[dict]:
    """Return the OpenAPI auth headers, or None if creds are missing.
    Uses the SAME env vars the trade adapter uses so the operator
    only manages one credential pair."""
    import os
    key = os.environ.get("WEBULL_APP_KEY")
    secret = os.environ.get("WEBULL_APP_SECRET")
    if not (key and secret):
        return None
    return {
        "X-Request-App-Key": key,
        "X-Request-App-Secret": secret,
        "Accept": "application/json",
    }


def _parse_webull_snapshot(ticker_req: str, payload: dict) -> Optional[dict]:
    """Webull OpenAPI /openapi/market-data/stock/snapshot response:
        { "result": true, "data": {
            "symbol": "AAPL", "latestPrice": 185.42,
            "quotes": { "bidPrice": 185.40, "askPrice": 185.44, ... }
        }}
    We also tolerate flatter shapes some regional endpoints emit."""
    if not payload:
        return None
    data = payload.get("data") or payload
    quotes = data.get("quotes") or data
    try:
        bid = float(quotes.get("bidPrice") or quotes.get("bid_price") or 0)
        ask = float(quotes.get("askPrice") or quotes.get("ask_price") or 0)
    except (TypeError, ValueError):
        return None
    last = None
    for k in ("latestPrice", "latest_price", "lastPrice", "last_price", "price"):
        v = data.get(k)
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
    """Pull an L1 snapshot from Webull's OpenAPI. Never raises — logs
    and returns None on any failure. Requires the L1 market-data
    subscription on the operator's OpenAPI plan."""
    headers = _webull_openapi_headers()
    if not headers:
        logger.warning(
            "spread webull skipped ticker=%s (WEBULL_APP_KEY/SECRET missing)",
            ticker,
        )
        return None
    url = _webull_openapi_base() + WEBULL_SNAPSHOT_PATH
    try:
        r = await client.get(
            url, params={"symbol": ticker.upper()}, headers=headers,
        )
        r.raise_for_status()
        j = r.json()
    except httpx.HTTPStatusError as e:
        # Surface auth / entitlement errors distinctly — operator needs
        # to know if the account isn't provisioned for L1.
        body = ""
        try:
            body = e.response.text[:200]
        except Exception:  # noqa: BLE001
            pass
        logger.warning(
            "spread webull HTTP %s ticker=%s body=%s",
            e.response.status_code, ticker, body,
        )
        return None
    except Exception as e:  # noqa: BLE001
        logger.warning("spread webull fetch failed ticker=%s err=%s",
                       ticker, e)
        return None
    if not (j.get("result") is True or j.get("data")):
        logger.warning("spread webull payload rejected ticker=%s payload=%s",
                       ticker, str(j)[:200])
        return None
    row = _parse_webull_snapshot(ticker, j)
    if not row:
        logger.warning(
            "spread webull parse failed ticker=%s (no bid/ask in payload)",
            ticker,
        )
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
