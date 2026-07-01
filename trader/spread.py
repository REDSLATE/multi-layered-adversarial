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
# Webull OpenAPI market-data snapshot — authenticated L1 quotes.
# Base URL from docs: `us-openapi-alb.uat.webullbroker.com`. This
# name reads "UAT" but is the documented base for individual/dev
# accounts; prod-tier operators can override via WEBULL_OPENAPI_BASE.
WEBULL_OPENAPI_BASE_DEFAULT = "https://us-openapi-alb.uat.webullbroker.com"
WEBULL_SNAPSHOT_PATH = "/openapi/market-data/stock/snapshot"
# Signature scheme (docs 2026-07-02):
#   string_to_sign = app_key + timestamp + nonce + METHOD + path + body
#   x-signature     = base64( HMAC-SHA1(app_secret, string_to_sign) )
# All 9 headers below are required; a missing header => 404 Route
# Not Found (Webull's dispatcher rejects unsigned requests at the
# router level, not the auth layer, which is why the earlier attempt
# looked like a bad URL).
WEBULL_SIGNATURE_VERSION = "1.0"
WEBULL_SIGNATURE_ALGO = "HMAC-SHA1"
WEBULL_API_VERSION = "v2"

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
    return (os.environ.get("WEBULL_OPENAPI_BASE") or
            WEBULL_OPENAPI_BASE_DEFAULT)


def _webull_creds() -> Optional[tuple[str, str, str]]:
    """Return (app_key, app_secret, access_token) or None if any is
    missing. Same env vars as the trade adapter — plus the one-time
    2FA-derived access token. If the access token env is unset we
    still return the tuple with an empty string; Webull's 404 on a
    missing token is explicit enough for the operator to notice."""
    import os
    key = os.environ.get("WEBULL_APP_KEY") or ""
    secret = os.environ.get("WEBULL_APP_SECRET") or ""
    token = os.environ.get("WEBULL_ACCESS_TOKEN") or ""
    # Strip any accidental quotes from the .env file (common footgun).
    key = key.strip().strip('"').strip("'")
    secret = secret.strip().strip('"').strip("'")
    token = token.strip().strip('"').strip("'")
    if not (key and secret):
        return None
    return key, secret, token


def _webull_sign(app_key: str, app_secret: str, timestamp: str,
                 nonce: str, method: str, path: str,
                 body: str = "") -> str:
    """Compute the base64(HMAC-SHA1) signature Webull expects.

    string_to_sign = app_key + timestamp + nonce + METHOD + path + body

    NB: `path` must be the request path WITHOUT the query string
    (verified against dev.to guide + Webull SDK source, 2026-07-02).
    `body` must be a minified JSON string for POSTs, or "" for GETs.
    """
    import hmac
    import hashlib
    import base64
    stt = f"{app_key}{timestamp}{nonce}{method.upper()}{path}{body}"
    digest = hmac.new(
        app_secret.encode("utf-8"),
        stt.encode("utf-8"),
        hashlib.sha1,
    ).digest()
    return base64.b64encode(digest).decode("utf-8")


def _webull_headers(app_key: str, app_secret: str, access_token: str,
                    method: str, path: str, body: str = "") -> dict:
    """Build the full 9-header set for a Webull OpenAPI request."""
    import uuid
    from datetime import datetime, timezone as _tz
    ts = datetime.now(_tz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    nonce = uuid.uuid4().hex
    sig = _webull_sign(app_key, app_secret, ts, nonce, method, path, body)
    return {
        "Accept": "application/json",
        "x-app-key": app_key,
        # Some Webull regional deployments require `x-app-secret` in
        # headers too (documented in the params panel on the snapshot
        # endpoint). Harmless when the server ignores it.
        "x-app-secret": app_secret,
        "x-timestamp": ts,
        "x-signature-version": WEBULL_SIGNATURE_VERSION,
        "x-signature-algorithm": WEBULL_SIGNATURE_ALGO,
        "x-signature-nonce": nonce,
        "x-access-token": access_token,
        "x-version": WEBULL_API_VERSION,
        "x-signature": sig,
    }


def _parse_webull_snapshot(ticker_req: str,
                           payload) -> Optional[dict]:
    """Webull returns an array of snapshot objects, one per requested
    symbol. Each object contains flat `bid`, `ask`, `price`, `symbol`
    fields (all as strings). We fetch one symbol per call for the
    per-symbol cache to stay simple; batching is a future opt."""
    if not payload:
        return None
    entry = None
    # Array form (per current docs)
    if isinstance(payload, list):
        want = ticker_req.upper()
        entry = next(
            (e for e in payload
             if (e.get("symbol") or "").upper() == want),
            payload[0] if payload else None,
        )
    # Object-wrapped form (some regional endpoints wrap in "data")
    elif isinstance(payload, dict):
        data = payload.get("data") or payload
        if isinstance(data, list):
            want = ticker_req.upper()
            entry = next(
                (e for e in data
                 if (e.get("symbol") or "").upper() == want),
                data[0] if data else None,
            )
        else:
            # Legacy shape with a nested `quotes` block
            quotes = data.get("quotes") if isinstance(data, dict) else None
            entry = quotes if isinstance(quotes, dict) else data
    if not isinstance(entry, dict):
        return None
    # Bid / ask can be top-level (current docs) or under `quotes` (legacy).
    def _pick(*keys):
        for k in keys:
            v = entry.get(k)
            if v is None or v == "":
                continue
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
        return None
    bid = _pick("bid", "bidPrice", "bid_price") or 0.0
    ask = _pick("ask", "askPrice", "ask_price") or 0.0
    last = _pick("price", "latestPrice", "latest_price",
                 "lastPrice", "last_price", "close")
    return _row_from_quote(
        symbol=ticker_req, lane="equity", source="webull",
        bid=bid, ask=ask, last=last,
    )


def _webull_category(ticker: str) -> str:
    """Map a symbol to Webull's required `category` param. We default
    to US_STOCK; operators tracking ETFs can hint via env
    `TRADER_EQUITY_SPREAD_ETFS` (comma-separated symbols)."""
    import os
    etfs = {
        s.strip().upper()
        for s in (os.environ.get("TRADER_EQUITY_SPREAD_ETFS") or "").split(",")
        if s.strip()
    }
    return "US_ETF" if ticker.upper() in etfs else "US_STOCK"


async def fetch_webull(client: httpx.AsyncClient,
                       ticker: str) -> Optional[dict]:
    """Pull an L1 snapshot from Webull's OpenAPI. Never raises — logs
    and returns None on any failure."""
    creds = _webull_creds()
    if not creds:
        logger.warning(
            "spread webull skipped ticker=%s (WEBULL_APP_KEY/SECRET missing)",
            ticker,
        )
        return None
    app_key, app_secret, access_token = creds
    if not access_token:
        logger.warning(
            "spread webull skipped ticker=%s (WEBULL_ACCESS_TOKEN missing — "
            "run POST /openapi/auth/token/create + approve 2FA in the "
            "Webull mobile app, then set env WEBULL_ACCESS_TOKEN)",
            ticker,
        )
        return None
    path = WEBULL_SNAPSHOT_PATH
    url = _webull_openapi_base() + path
    headers = _webull_headers(app_key, app_secret, access_token, "GET", path)
    params = {
        "symbols": ticker.upper(),
        "category": _webull_category(ticker),
    }
    try:
        r = await client.get(url, params=params, headers=headers)
        r.raise_for_status()
        j = r.json()
    except httpx.HTTPStatusError as e:
        body = ""
        try:
            body = e.response.text[:240]
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
    row = _parse_webull_snapshot(ticker, j)
    if not row:
        logger.warning(
            "spread webull parse failed ticker=%s payload=%s",
            ticker, str(j)[:240],
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
