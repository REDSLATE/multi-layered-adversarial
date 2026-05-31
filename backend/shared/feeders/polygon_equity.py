"""Polygon equity feeder — DAILY OHLCV source for US equities.

Why this exists:
  Polygon's grouped-daily aggregates endpoint returns the entire US
  equity market in ONE HTTP call (~12k rows on a typical session).
  At the operator's plan tier (Starter / Aggregates-only — verified
  2026-05-31), per-symbol daily and grouped-daily are authorized;
  intraday minute aggregates and real-time last-trade are NOT.

  So Polygon owns `tf=1d` in MC's bar federation; Finnhub continues
  to own `tf=5m`. The daily snapshot system reads both via per-tf
  blocks — they don't conflate.

Worker pattern:
  * Async polling task spawned in FastAPI lifespan (mirrors the
    Finnhub feeder).
  * Once per `POLYGON_POLL_INTERVAL_SEC` (default 1h):
      - Skip if today isn't a NYSE trading day.
      - Skip until N minutes after market close (the bars only
        finalize after ~16:30 ET on Polygon).
      - Otherwise call /v2/aggs/grouped/locale/us/market/stocks/{date}
        for the most-recent NYSE trading day. Upsert each row into
        `shared_ohlcv_bars` with `source="polygon"`, `tf="1d"`.
  * Idempotent: re-running the same day's grouped pull upserts on
    `(source, symbol, tf, ts)` — the same key the existing ingest
    pipeline already uses.

Doctrine pin: EVIDENCE only. No code path modifies execution
authority. Ingest path here mirrors the Finnhub feeder's direct
DB write — no HTTP self-call, no broker keys touched.

Configuration (backend/.env):
  POLYGON_API_KEY              external Polygon token
  POLYGON_FEEDER_ENABLED       "true" to enable (default true if key set)
  POLYGON_POLL_INTERVAL_SEC    default 3600 (1h)
  POLYGON_CLOSE_BUFFER_MIN     wait this many minutes after market
                               close before pulling that day's bars.
                               default 30. Polygon's grouped endpoint
                               finalizes ~T+15min; 30 is safe.

Failure mode: any error → row in `feeder_health_audit`; the worker
sleeps and tries again. Missing API key short-circuits to no-op
with one health-audit row.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import date, datetime, timezone
from typing import Any, Optional

import httpx

from db import db
from namespaces import SHARED_OHLCV_BARS
from shared.feeders.feeder_health import record_feeder_health
from shared.snapshots.nyse_calendar import (
    is_trading_day,
    market_day_today,
    now_eastern,
)


logger = logging.getLogger(__name__)


POLYGON_BASE_URL = "https://api.polygon.io"
PROVIDER = "polygon_equity"
FEEDER_SOURCE = "polygon"
DEFAULT_TF = "1d"

# Polygon's grouped endpoint finalizes a few minutes after close.
# 30 minutes after 16:00 ET (16:30 ET) is the safe earliest pull.
DEFAULT_CLOSE_BUFFER_MIN = 30


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ──────────────────────── HTTP client (lazy) ────────────────────────


_client: Optional[httpx.AsyncClient] = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            base_url=POLYGON_BASE_URL,
            timeout=httpx.Timeout(connect=5.0, read=60.0, write=10.0, pool=5.0),
        )
    return _client


async def _close_client() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


# ──────────────────────── config ────────────────────────


def _read_config() -> dict[str, Any]:
    api_key = (os.environ.get("POLYGON_API_KEY") or "").strip()
    enabled_env = (os.environ.get("POLYGON_FEEDER_ENABLED") or "").strip().lower()
    # Default-on when key is present; explicit "false" disables.
    if enabled_env == "false":
        enabled = False
    elif enabled_env == "true":
        enabled = True
    else:
        enabled = bool(api_key)
    return {
        "api_key": api_key,
        "enabled": enabled,
        "interval": int(os.environ.get("POLYGON_POLL_INTERVAL_SEC", "3600")),
        "close_buffer_min": int(
            os.environ.get("POLYGON_CLOSE_BUFFER_MIN", str(DEFAULT_CLOSE_BUFFER_MIN))
        ),
    }


# ──────────────────────── grouped-daily fetch ────────────────────────


async def fetch_grouped_daily(
    bar_date: date, api_key: str,
) -> Optional[dict[str, Any]]:
    """Pull the entire US equity market for one day. Returns the JSON
    payload on success, None on error (after writing one
    feeder_health_audit row)."""
    path = f"/v2/aggs/grouped/locale/us/market/stocks/{bar_date.isoformat()}"
    try:
        resp = await _get_client().get(
            path,
            params={"adjusted": "true", "apiKey": api_key},
        )
        if resp.status_code != 200:
            await record_feeder_health(
                provider=PROVIDER, endpoint=path,
                status_code=resp.status_code,
                error_type="http_status_error",
                message=resp.text[:500],
                context={"date": bar_date.isoformat()},
            )
            return None
        data = resp.json()
        if data.get("status") not in ("OK", "DELAYED"):
            await record_feeder_health(
                provider=PROVIDER, endpoint=path,
                status_code=resp.status_code,
                error_type="api_error",
                message=str(data)[:500],
                context={"date": bar_date.isoformat(), "status": data.get("status")},
            )
            return None
        return data
    except Exception as exc:  # noqa: BLE001 — bounded network call
        await record_feeder_health(
            provider=PROVIDER, endpoint=path,
            status_code=None,
            error_type="request_error",
            message=f"{type(exc).__name__}: {exc}",
            context={"date": bar_date.isoformat()},
        )
        return None


def _row_to_bar(row: dict[str, Any], bar_date: date) -> Optional[dict[str, Any]]:
    """Convert one Polygon grouped-aggs row into our `shared_ohlcv_bars`
    schema. Returns None if the row is malformed."""
    symbol = (row.get("T") or "").upper().strip()
    if not symbol or len(symbol) > 16:
        return None
    o = row.get("o")
    hi = row.get("h")
    lo = row.get("l")
    c = row.get("c")
    v = row.get("v")
    if None in (o, hi, lo, c):
        return None
    # Polygon `t` is end-of-day epoch-ms. Use the trading-day's UTC
    # midnight as the canonical bar timestamp (matches the existing
    # convention in tests for daily bars).
    ts_iso = datetime.combine(
        bar_date, datetime.min.time(), tzinfo=timezone.utc,
    ).isoformat()
    return {
        "source": FEEDER_SOURCE,
        "symbol": symbol,
        "tf": DEFAULT_TF,
        "ts": ts_iso,
        "o": float(o), "h": float(hi), "l": float(lo), "c": float(c),
        "v": float(v or 0.0),
        "vwap": row.get("vw"),
        "trades": row.get("n"),
        "ingested_at": _now_iso(),
        "feeder": PROVIDER,
    }


async def _upsert_bars(bars: list[dict[str, Any]]) -> int:
    """Upsert bars into `shared_ohlcv_bars`. Returns count written.
    Idempotent: re-running the same day's pull no-ops on row level."""
    written = 0
    for bar in bars:
        try:
            await db[SHARED_OHLCV_BARS].update_one(
                {
                    "source": bar["source"],
                    "symbol": bar["symbol"],
                    "tf": bar["tf"],
                    "ts": bar["ts"],
                },
                {"$set": bar},
                upsert=True,
            )
            written += 1
        except Exception as exc:  # noqa: BLE001 — one bad row mustn't tank the batch
            await record_feeder_health(
                provider=PROVIDER, endpoint="_upsert_bars",
                status_code=None, error_type="db_error",
                message=f"{type(exc).__name__}: {exc}",
                context={"symbol": bar.get("symbol"), "ts": bar.get("ts")},
            )
    return written


# ──────────────────────── one-shot pull ────────────────────────


async def pull_for_date(bar_date: date, api_key: str) -> dict[str, Any]:
    """Public entry point — pull and persist one day's grouped daily
    aggregates for the entire US equity market.

    Idempotent on `(source, symbol, tf, ts)`. Returns a summary dict.
    """
    payload = await fetch_grouped_daily(bar_date, api_key)
    if payload is None:
        return {
            "ok": False, "date": bar_date.isoformat(),
            "rows_fetched": 0, "rows_persisted": 0,
            "reason": "fetch_failed_see_feeder_health_audit",
        }
    raw = payload.get("results") or []
    bars: list[dict[str, Any]] = []
    skipped = 0
    for row in raw:
        bar = _row_to_bar(row, bar_date)
        if bar is None:
            skipped += 1
        else:
            bars.append(bar)
    written = await _upsert_bars(bars)
    summary = {
        "ok": True,
        "date": bar_date.isoformat(),
        "rows_fetched": len(raw),
        "rows_persisted": written,
        "rows_skipped_malformed": skipped,
        "polygon_query_count": payload.get("queryCount"),
        "polygon_results_count": payload.get("resultsCount"),
    }
    logger.info("polygon grouped-daily pulled: %s", summary)
    return summary


# ──────────────────────── worker loop ────────────────────────


_task: Optional[asyncio.Task] = None
_stop_flag: bool = False


def _safe_to_pull(now_et: datetime, close_buffer_min: int) -> Optional[date]:
    """Decide whether NOW is a good time to pull, and which date for.

    Rules:
      - If today is a NYSE trading day AND wall-clock is past
        16:00 ET + buffer: pull TODAY's grouped daily.
      - Otherwise: walk backward from today to the most-recent
        trading day strictly BEFORE today and pull that.
      - Never pulls a NON-trading day's date — Polygon returns
        empty for those.
    """
    from datetime import timedelta
    today = now_et.date()
    minutes = now_et.hour * 60 + now_et.minute
    close_minutes = 16 * 60 + close_buffer_min
    if is_trading_day(today) and minutes >= close_minutes:
        return today
    # Walk back to the previous trading day strictly before today.
    cursor = today - timedelta(days=1)
    while not is_trading_day(cursor):
        cursor -= timedelta(days=1)
        if (today - cursor).days > 60:  # pragma: no cover — safety stop
            return None
    return cursor


async def _tick(api_key: str, close_buffer_min: int) -> dict[str, Any]:
    """One pass of the worker loop. Public for testability."""
    now_et = now_eastern()
    target = _safe_to_pull(now_et, close_buffer_min)
    if target is None:
        return {"skipped": True, "reason": "no_target_date"}
    # Check if we already have bars for this target (idempotency shortcut).
    already = await db[SHARED_OHLCV_BARS].count_documents({
        "source": FEEDER_SOURCE,
        "tf": DEFAULT_TF,
        "ts": datetime.combine(
            target, datetime.min.time(), tzinfo=timezone.utc,
        ).isoformat(),
    })
    if already >= 5000:
        # Polygon grouped-daily returns ~12k rows; if we already have
        # >5k for this target, we've successfully pulled it. Skip.
        return {
            "skipped": True, "reason": "already_pulled",
            "target_date": target.isoformat(), "rows_in_db": already,
        }
    return await pull_for_date(target, api_key)


async def _worker_loop() -> None:
    global _stop_flag
    cfg = _read_config()
    logger.info(
        "polygon_equity worker loop: interval=%ss close_buffer_min=%s",
        cfg["interval"], cfg["close_buffer_min"],
    )
    while not _stop_flag:
        try:
            cfg = _read_config()
            if not cfg["enabled"] or not cfg["api_key"]:
                await record_feeder_health(
                    provider=PROVIDER, endpoint="(boot)",
                    status_code=None, error_type="configuration",
                    message="POLYGON_API_KEY missing or POLYGON_FEEDER_ENABLED=false",
                )
                await asyncio.sleep(max(cfg["interval"], 300))
                continue
            summary = await _tick(cfg["api_key"], cfg["close_buffer_min"])
            logger.info("polygon_equity tick: %s", summary)
        except Exception as exc:  # noqa: BLE001
            logger.warning("polygon_equity loop crashed: %s", exc)
            await record_feeder_health(
                provider=PROVIDER, endpoint="_worker_loop",
                status_code=None, error_type="worker_crash",
                message=str(exc)[:500],
            )
        try:
            await asyncio.sleep(cfg["interval"])
        except asyncio.CancelledError:
            break


def start_worker_if_enabled() -> None:
    """Spawn the polling task. Idempotent — re-callable on hot reload."""
    global _task, _stop_flag
    if _task is not None and not _task.done():
        return
    cfg = _read_config()
    if not cfg["enabled"]:
        logger.info(
            "polygon_equity worker disabled "
            "(POLYGON_FEEDER_ENABLED=false or POLYGON_API_KEY missing)"
        )
        return
    _stop_flag = False
    _task = asyncio.create_task(_worker_loop(), name="polygon_equity_worker")
    logger.info("polygon_equity worker started (interval=%ss)", cfg["interval"])


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


__all__ = (
    "PROVIDER",
    "FEEDER_SOURCE",
    "DEFAULT_TF",
    "fetch_grouped_daily",
    "pull_for_date",
    "start_worker_if_enabled",
    "stop_worker",
    "_tick",
    "_row_to_bar",
    "_safe_to_pull",
)
