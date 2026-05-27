"""Finnhub equity feeder — PRIMARY OHLCV source for US equities.

Worker pattern (per integration playbook v2):
  * Async polling task spawned in FastAPI lifespan.
  * Fetches /stock/candle for every symbol in `patterns_universe`.
  * POSTs bars to the internal /api/ingest/ohlcv/batch endpoint via
    in-process call (re-uses the existing tripwire-pinned ingest path).
  * Weekly /stock/profile2 refresh populates `symbol_metadata` with
    float, market cap, sector — feeds the small-cap qualifier.

Doctrine pin: this module writes EVIDENCE only. No code path here
modifies execution authority. The OHLCV ingest schema (existing
tripwire) continues to reject any `may_execute` field.

Configuration (backend/.env):
  FINNHUB_API_KEY            external Finnhub token (free signup)
  FINNHUB_FEEDER_TOKEN       internal token, matches what /api/ingest/ohlcv accepts
  FINNHUB_POLL_INTERVAL_SEC  default 300
  FINNHUB_TIMEFRAME          default "5"  (Finnhub resolution code)
  FINNHUB_ENABLED            "true" to enable; default false so missing key fails soft

Failure mode: any error → row in feeder_health_audit; the worker
continues to the next symbol. A missing API key short-circuits the
worker into a no-op (with one health-audit row to surface the misconfig).
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from db import db
from namespaces import SYMBOL_METADATA, PATTERNS_UNIVERSE
from shared.feeders.feeder_health import record_feeder_health


logger = logging.getLogger(__name__)


FINNHUB_BASE_URL = "https://finnhub.io/api/v1"
PROVIDER = "finnhub_equity"
FEEDER_SOURCE = "finnhub_equity"  # matches FEEDERS["finnhub_equity"]
PROFILE_REFRESH_SECS = 7 * 24 * 60 * 60   # 7 days
MAX_BARS_PER_REQUEST = 100


# Finnhub `resolution` → our internal tf names (existing TIMEFRAMES).
_RES_TO_TF = {
    "1": "1m", "5": "5m", "15": "15m",
    "60": "1h", "240": "4h", "D": "1d",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _seconds_per_tf(resolution: str) -> int:
    mapping = {
        "1": 60, "5": 300, "15": 900,
        "60": 3_600, "240": 14_400, "D": 86_400,
    }
    return mapping.get(resolution, 300)


# ──────────────────────── HTTP client (lazy) ────────────────────────

_client: Optional[httpx.AsyncClient] = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            base_url=FINNHUB_BASE_URL,
            timeout=httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0),
        )
    return _client


async def _close_client() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


# ──────────────────────── single-shot fetchers ────────────────────────

async def fetch_candles(
    symbol: str, resolution: str, frm: int, to: int, api_key: str,
) -> Optional[dict[str, Any]]:
    """Fetch candles. Returns the JSON payload on success, None on
    error (after writing a feeder_health_audit row)."""
    try:
        resp = await _get_client().get(
            "/stock/candle",
            params={
                "symbol": symbol, "resolution": resolution,
                "from": frm, "to": to, "token": api_key,
            },
        )
    except httpx.RequestError as exc:
        await record_feeder_health(
            provider=PROVIDER, endpoint="/stock/candle", status_code=None,
            error_type="request_error", message=str(exc),
            context={"symbol": symbol, "resolution": resolution},
        )
        return None

    if resp.status_code == 429:
        await record_feeder_health(
            provider=PROVIDER, endpoint="/stock/candle",
            status_code=429, error_type="rate_limit",
            message=f"429 — retry-after={resp.headers.get('Retry-After')}",
            context={"symbol": symbol},
        )
        return None
    if resp.status_code >= 400:
        await record_feeder_health(
            provider=PROVIDER, endpoint="/stock/candle",
            status_code=resp.status_code, error_type="http_status_error",
            message=resp.text[:500],
            context={"symbol": symbol},
        )
        return None

    payload = resp.json()
    if not isinstance(payload, dict) or payload.get("s") != "ok":
        await record_feeder_health(
            provider=PROVIDER, endpoint="/stock/candle",
            status_code=resp.status_code, error_type="api_error",
            message=f"s={payload.get('s') if isinstance(payload, dict) else payload!r}",
            context={"symbol": symbol},
        )
        return None
    return payload


def candles_to_bars(
    symbol: str, resolution: str, payload: dict[str, Any],
) -> list[dict[str, Any]]:
    """Transform a Finnhub /stock/candle payload into OHLCVBarIn shape.

    `s` is "ok" precondition (checked by caller). Mismatched array
    lengths skip those rows defensively."""
    tf = _RES_TO_TF.get(resolution)
    if tf is None:
        return []
    t = payload.get("t") or []
    o = payload.get("o") or []
    h = payload.get("h") or []
    low = payload.get("l") or []
    c = payload.get("c") or []
    v = payload.get("v") or []
    n = min(len(t), len(o), len(h), len(low), len(c), len(v))
    bars: list[dict[str, Any]] = []
    for i in range(n):
        ts_iso = (
            datetime.fromtimestamp(int(t[i]), tz=timezone.utc).isoformat()
        )
        bars.append({
            "source": FEEDER_SOURCE,
            "symbol": symbol.upper(),
            "tf": tf,
            "ts": ts_iso,
            "o": float(o[i]),
            "h": float(h[i]),
            "l": float(low[i]),
            "c": float(c[i]),
            "v": float(v[i]) if v[i] is not None else 0.0,
        })
    return bars


async def fetch_profile(symbol: str, api_key: str) -> Optional[dict[str, Any]]:
    try:
        resp = await _get_client().get(
            "/stock/profile2", params={"symbol": symbol, "token": api_key},
        )
    except httpx.RequestError as exc:
        await record_feeder_health(
            provider=PROVIDER, endpoint="/stock/profile2", status_code=None,
            error_type="request_error", message=str(exc),
            context={"symbol": symbol},
        )
        return None
    if resp.status_code == 429:
        await record_feeder_health(
            provider=PROVIDER, endpoint="/stock/profile2",
            status_code=429, error_type="rate_limit",
            message=f"429 — retry-after={resp.headers.get('Retry-After')}",
            context={"symbol": symbol},
        )
        return None
    if resp.status_code >= 400:
        await record_feeder_health(
            provider=PROVIDER, endpoint="/stock/profile2",
            status_code=resp.status_code, error_type="http_status_error",
            message=resp.text[:500], context={"symbol": symbol},
        )
        return None
    payload = resp.json()
    if not isinstance(payload, dict) or not payload:
        await record_feeder_health(
            provider=PROVIDER, endpoint="/stock/profile2",
            status_code=resp.status_code, error_type="api_error",
            message="empty profile", context={"symbol": symbol},
        )
        return None
    return payload


async def upsert_symbol_metadata(symbol: str, profile: dict[str, Any]) -> None:
    """Persist /stock/profile2 fields into symbol_metadata. Powers the
    small_cap_qualified flag on the pattern detector automatically."""
    doc = {
        "symbol": symbol.upper(),
        "name": profile.get("name"),
        "exchange": profile.get("exchange"),
        "country": profile.get("country"),
        "currency": profile.get("currency"),
        "sector": profile.get("finnhubIndustry"),
        "ipo": profile.get("ipo"),
        "market_cap_millions": profile.get("marketCapitalization"),
        # Finnhub returns share count in MILLIONS — match our downstream contract.
        "float_shares_millions": profile.get("shareOutstanding"),
        "source": PROVIDER,
        "refreshed_at": _now_iso(),
    }
    await db[SYMBOL_METADATA].update_one(
        {"symbol": doc["symbol"]}, {"$set": doc}, upsert=True,
    )


# ──────────────────────── ingest path ────────────────────────

async def ingest_bars_into_mc(bars: list[dict[str, Any]]) -> int:
    """Write the bars directly via the same code path the public
    /api/ingest/ohlcv/batch endpoint uses. Bypasses HTTP self-call but
    keeps the persistence + snapshot-recompute behaviour identical.

    Doctrine: same idempotent upsert (source, symbol, tf, ts).
    """
    if not bars:
        return 0
    # Import here to avoid a cycle (technicals imports namespaces).
    from shared.technicals import _persist_bar, _recompute_snapshot
    affected: set[tuple[str, str, str]] = set()
    for b in bars:
        await _persist_bar(dict(b))
        affected.add((b["source"], b["symbol"], b["tf"]))
    for source, symbol, tf in affected:
        await _recompute_snapshot(source, symbol, tf)
    return len(bars)


# ──────────────────────── watchlist helpers ────────────────────────

async def universe_symbols() -> list[str]:
    """Return active symbols from `patterns_universe`. Empty list →
    worker no-ops gracefully."""
    rows = await db[PATTERNS_UNIVERSE].find(
        {"active": {"$ne": False}}, {"_id": 0, "symbol": 1},
    ).to_list(1000)
    return [r["symbol"] for r in rows if r.get("symbol")]


# ──────────────────────── polling worker ────────────────────────

_task: Optional[asyncio.Task] = None
_stop_flag = False


def _read_config() -> dict[str, Any]:
    return {
        "enabled": os.environ.get("FINNHUB_ENABLED", "").lower() == "true",
        "api_key": os.environ.get("FINNHUB_API_KEY", "").strip(),
        "interval": int(os.environ.get("FINNHUB_POLL_INTERVAL_SEC", "300")),
        "resolution": os.environ.get("FINNHUB_TIMEFRAME", "5"),
    }


async def _poll_once(api_key: str, resolution: str) -> dict[str, Any]:
    """One polling cycle. Returns a summary dict for diagnostics."""
    symbols = await universe_symbols()
    if not symbols:
        return {"symbols": 0, "bars_ingested": 0, "profile_refreshes": 0}

    # Pull last MAX_BARS_PER_REQUEST candles per symbol.
    now_ts = int(datetime.now(timezone.utc).timestamp())
    frm_ts = now_ts - MAX_BARS_PER_REQUEST * _seconds_per_tf(resolution)

    total_bars = 0
    for symbol in symbols:
        payload = await fetch_candles(symbol, resolution, frm_ts, now_ts, api_key)
        if not payload:
            continue
        bars = candles_to_bars(symbol, resolution, payload)
        total_bars += await ingest_bars_into_mc(bars)

    # Weekly profile refresh — per-symbol last_refreshed_at check.
    refreshes = 0
    now_iso = _now_iso()
    for symbol in symbols:
        existing = await db[SYMBOL_METADATA].find_one(
            {"symbol": symbol}, {"_id": 0, "refreshed_at": 1},
        )
        if existing:
            try:
                last = datetime.fromisoformat(
                    existing.get("refreshed_at", "").replace("Z", "+00:00"),
                )
                if (datetime.now(timezone.utc) - last).total_seconds() < PROFILE_REFRESH_SECS:
                    continue
            except (ValueError, AttributeError):
                pass
        profile = await fetch_profile(symbol, api_key)
        if profile:
            await upsert_symbol_metadata(symbol, profile)
            refreshes += 1

    return {
        "symbols": len(symbols),
        "bars_ingested": total_bars,
        "profile_refreshes": refreshes,
        "ts": now_iso,
    }


async def _worker_loop() -> None:
    global _stop_flag
    while not _stop_flag:
        cfg = _read_config()
        if not cfg["enabled"]:
            await asyncio.sleep(60)
            continue
        if not cfg["api_key"]:
            await record_feeder_health(
                provider=PROVIDER, endpoint="(boot)", status_code=None,
                error_type="configuration", message="FINNHUB_API_KEY missing",
            )
            await asyncio.sleep(300)
            continue
        try:
            summary = await _poll_once(cfg["api_key"], cfg["resolution"])
            logger.info("finnhub_equity poll: %s", summary)
        except Exception as exc:  # noqa: BLE001
            logger.warning("finnhub_equity poll crashed: %s", exc)
            await record_feeder_health(
                provider=PROVIDER, endpoint="_worker_loop", status_code=None,
                error_type="worker_crash", message=str(exc)[:500],
            )
        await asyncio.sleep(cfg["interval"])


def start_worker_if_enabled() -> None:
    """Spawn the polling task. Idempotent — re-callable on hot reload."""
    global _task, _stop_flag
    if _task is not None and not _task.done():
        return
    cfg = _read_config()
    if not cfg["enabled"]:
        logger.info("finnhub_equity worker disabled (FINNHUB_ENABLED!=true)")
        return
    _stop_flag = False
    _task = asyncio.create_task(_worker_loop(), name="finnhub_equity_worker")
    logger.info("finnhub_equity worker started (interval=%ss)", cfg["interval"])


async def stop_worker() -> None:
    global _task, _stop_flag
    _stop_flag = True
    if _task is not None and not _task.done():
        _task.cancel()
        try:
            await _task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    _task = None
    await _close_client()
