"""StockFit Quotes integration — EOD closing-price source for the
shadow-outcome pipeline.

Operator directive (2026-02-19, evening): "Can we have it change the
number without real cash being involved? Just EOD closing tickers?"
The brain's `0/100` LEARNING counter only moves on real broker fills.
StockFit gives us SEC-derived EOD closes (free tier: 50 req/min, 750
req/day, 2 years of history). That lets us synthesize an `outcome_join`
envelope for every intent emitted today — what WOULD HAVE happened if
the order filled at intent-time and closed at session close.

Doctrine guardrails:
  * Network calls are dispatched via `httpx.AsyncClient` — no blocking
    of the FastAPI event loop.
  * The client is a process-wide singleton + small in-process cache
    (one entry per `symbol@yyyy-mm-dd`) so a re-run of the
    shadow-close engine doesn't re-burn the API quota.
  * Conservative client-side rate limit: 45 req/minute (under the
    50/min Free-tier ceiling) so a misconfigured cron can't get us
    banned. Daily 750-req cap is operator-monitored via the
    `X-RateLimit-Remaining-Day` header (logged).

API key comes from `STOCKFIT_API_KEY` env var. Missing key → all
methods return None, which the shadow-close engine treats as "skip
this symbol" — never raises a fatal error during background runs.

Server base: https://api.stockfit.io/v1
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import deque
from datetime import datetime, timezone
from typing import Iterable, Optional

import httpx

logger = logging.getLogger("risedual.stockfit")

_BASE_URL = "https://api.stockfit.io/v1"
_FREE_TIER_PER_MIN = 45  # conservative under the 50/min Free ceiling
_TIMEOUT_SEC = 10.0
_CACHE_TTL_SEC = 6 * 3600  # cache an EOD close for 6h (well past close)

# Daily-budget safety buffer. The Free tier caps at 750/day. We stop
# issuing calls once we drop to this threshold so an unexpected cron
# or operator-loop can't lock the operator out of StockFit for the
# rest of the day. Override via env if you upgrade tiers.
_DAILY_RESERVE_FLOOR = int(os.environ.get("STOCKFIT_DAILY_RESERVE_FLOOR", "50"))

# Last observed `X-RateLimit-Remaining-Day` value from a StockFit
# response. Set on every successful call. Read by the budget guard
# below so we can refuse new requests when we're near the floor.
_last_remaining_day: Optional[int] = None

_client: Optional[httpx.AsyncClient] = None
_client_lock = asyncio.Lock()

# (symbol, yyyy-mm-dd) → (close_price, expiry_ts)
_quote_cache: dict[tuple[str, str], tuple[float, float]] = {}

# Sliding-window rate limiter: a deque of request timestamps (epoch
# seconds). On each call we drop entries older than 60s and refuse if
# the window is at the cap.
_recent_ts: deque[float] = deque(maxlen=_FREE_TIER_PER_MIN * 2)
_rate_lock = asyncio.Lock()


def _api_key() -> Optional[str]:
    return (os.environ.get("STOCKFIT_API_KEY") or "").strip() or None


async def _get_client() -> Optional[httpx.AsyncClient]:
    """Singleton httpx async client. Returns None when the API key
    isn't configured so callers can skip cleanly without raising."""
    global _client
    if not _api_key():
        return None
    if _client is not None:
        return _client
    async with _client_lock:
        if _client is None:
            _client = httpx.AsyncClient(
                base_url=_BASE_URL,
                timeout=_TIMEOUT_SEC,
                headers={
                    "Authorization": f"Bearer {_api_key()}",
                    "Accept": "application/json",
                },
            )
            logger.info("StockFit async client initialized base=%s", _BASE_URL)
    return _client


async def _gate_rate_limit() -> bool:
    """Returns True if a request can proceed. Drops the request
    cleanly when the local window is full (caller should treat
    None-on-return same as a 429).

    Also enforces the daily-budget reserve floor (default 50 reqs).
    Once StockFit's `X-RateLimit-Remaining-Day` drops to that floor,
    further calls are refused locally — we'd rather skip a shadow-
    close run than burn the operator's entire daily quota on one
    badly-formed cron tick.
    """
    if _last_remaining_day is not None and _last_remaining_day <= _DAILY_RESERVE_FLOOR:
        logger.warning(
            "StockFit daily budget reserve floor reached "
            "(remaining=%s ≤ floor=%s). Refusing further calls today.",
            _last_remaining_day, _DAILY_RESERVE_FLOOR,
        )
        return False
    async with _rate_lock:
        now = time.time()
        while _recent_ts and (now - _recent_ts[0]) > 60.0:
            _recent_ts.popleft()
        if len(_recent_ts) >= _FREE_TIER_PER_MIN:
            return False
        _recent_ts.append(now)
        return True


def _absorb_budget_header(headers: dict) -> None:
    """Read the X-RateLimit-Remaining-Day header off any StockFit
    response and update the module-level cache so subsequent calls
    can short-circuit at the reserve floor."""
    global _last_remaining_day
    val = headers.get("X-RateLimit-Remaining-Day") or headers.get("x-ratelimit-remaining-day")
    if val is None:
        return
    try:
        _last_remaining_day = int(val)
    except (TypeError, ValueError):
        pass


def get_last_daily_remaining() -> Optional[int]:
    """Operator-facing: what does StockFit say we have left today?"""
    return _last_remaining_day


async def get_eod_quote(symbol: str) -> Optional[dict]:
    """Fetch the latest EOD OHLCV envelope for one symbol.

    Returns:
        {"ts": ISO8601, "open": .., "high": .., "low": ..,
         "close": .., "adjClose": .., "volume": ..}
        OR None if the API isn't configured, the symbol is unknown,
        we hit a local rate-limit gate, or the call errored.
    """
    sym = (symbol or "").upper().strip()
    if not sym:
        return None
    cached = _quote_cache.get((sym, datetime.now(timezone.utc).strftime("%Y-%m-%d")))
    if cached:
        price, exp = cached
        if exp > time.time():
            return {"close": price, "_cached": True}

    client = await _get_client()
    if client is None:
        return None
    if not await _gate_rate_limit():
        logger.warning("StockFit local rate-limit gate refused get_eod_quote(%s)", sym)
        return None
    try:
        r = await client.get("/api/price/quote", params={"symbols": sym})
        if r.status_code == 429:
            logger.warning("StockFit /price/quote returned 429 for %s", sym)
            return None
        r.raise_for_status()
        body = r.json()
    except Exception as e:  # noqa: BLE001
        logger.warning("StockFit get_eod_quote(%s) failed: %s", sym, e)
        return None

    row = (body or {}).get(sym) if isinstance(body, dict) else None
    if not isinstance(row, dict):
        return None
    close = row.get("close") or row.get("adjClose")
    if close is None:
        return None
    _quote_cache[(sym, datetime.now(timezone.utc).strftime("%Y-%m-%d"))] = (
        float(close), time.time() + _CACHE_TTL_SEC,
    )
    return row


async def get_eod_quotes_batch(symbols: Iterable[str]) -> dict[str, dict]:
    """Batch variant of `get_eod_quote`.

    StockFit's `/price/quote` endpoint natively supports a
    comma-separated `symbols=` query — one HTTP call returns one row
    per symbol. We use the batch shape so a shadow-close run covering
    50 unique symbols counts as ONE request against the 750/day cap
    instead of 50.

    Returns a dict mapping ticker → row (or skips the ticker entirely
    on missing data).
    """
    syms = sorted({(s or "").upper().strip() for s in symbols if s})
    syms = [s for s in syms if s]
    if not syms:
        return {}
    client = await _get_client()
    if client is None:
        return {}
    if not await _gate_rate_limit():
        logger.warning(
            "StockFit local rate-limit gate refused batch quote (%d symbols)",
            len(syms),
        )
        return {}
    try:
        r = await client.get(
            "/api/price/quote", params={"symbols": ",".join(syms)},
        )
        if r.status_code == 429:
            logger.warning("StockFit /price/quote 429 on batch of %d", len(syms))
            return {}
        # Surface the daily-budget remaining count to operator-readable logs.
        rem_day = r.headers.get("X-RateLimit-Remaining-Day")
        if rem_day is not None:
            logger.info("StockFit daily budget remaining: %s", rem_day)
        _absorb_budget_header(dict(r.headers))
        r.raise_for_status()
        body = r.json()
    except Exception as e:  # noqa: BLE001
        logger.warning("StockFit batch quote failed: %s", e)
        return {}
    if not isinstance(body, dict):
        return {}
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out: dict[str, dict] = {}
    for sym in syms:
        row = body.get(sym)
        if isinstance(row, dict) and (row.get("close") is not None):
            out[sym] = row
            _quote_cache[(sym, today)] = (
                float(row["close"]), time.time() + _CACHE_TTL_SEC,
            )
    return out


async def get_history(
    symbol: str, from_date: str, to_date: str,
) -> list[tuple[int, float]]:
    """Fetch daily OHLC bars between two dates (YYYY-MM-DD).

    Returns a list of `(timestamp_ms, close_price)` tuples — matches
    StockFit's native response shape so callers don't have to remap.
    Empty list on any error or missing config.
    """
    sym = (symbol or "").upper().strip()
    if not sym:
        return []
    client = await _get_client()
    if client is None:
        return []
    if not await _gate_rate_limit():
        return []
    try:
        r = await client.get(
            "/api/price/history",
            params={"symbol": sym, "from": from_date, "to": to_date},
        )
        if r.status_code == 429:
            logger.warning("StockFit /price/history 429 for %s", sym)
            return []
        r.raise_for_status()
        body = r.json()
    except Exception as e:  # noqa: BLE001
        logger.warning("StockFit get_history(%s) failed: %s", sym, e)
        return []
    data = (body or {}).get("data") or []
    out: list[tuple[int, float]] = []
    for row in data:
        if isinstance(row, (list, tuple)) and len(row) >= 2:
            ts, px = row[0], row[1]
            if px is not None:
                out.append((int(ts), float(px)))
    return out


def reset_stockfit_for_tests() -> None:
    """Tests rebind the singleton + cache. Production never calls this."""
    global _client
    _client = None
    _quote_cache.clear()
    _recent_ts.clear()
