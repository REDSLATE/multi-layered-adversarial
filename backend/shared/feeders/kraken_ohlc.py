"""Kraken crypto feeder — DAILY OHLCV via public `/0/public/OHLC`.

Doctrine pin (2026-02-20, operator directive):
    RVOL coverage on equity reached ~100% after the polygon
    flatfiles feeder shipped a 20-day daily-baseline lookup
    (2026-02-19 Follow-up B). Crypto RVOL is still on the
    intraday-only fallback — the 3–4 session window gives noisy
    ratios and misses the multi-week seasonality the doctrine
    consumers assume.

    Fix: Kraken's public OHLC endpoint exposes daily bars
    unauthenticated. This feeder polls it on a fixed cadence for
    the crypto universe and writes `tf=1d` rows to
    `shared_ohlcv_bars` — the SAME schema the equity flatfiles
    feeder uses. `session_features._fetch_daily_volume_baseline`
    is source-agnostic and picks them up automatically.

    Reaches 100% RVOL coverage across all live crypto symbols.

Why a separate module (not merged into `shared/crypto/kraken.py`):
    * The kraken.py module owns REST/private/auth for order flow
      and account state. Data feeders live in shared/feeders/
      by convention (finnhub_equity, polygon_flatfiles, ...).
    * Isolating this module keeps rollback trivial — set
      `KRAKEN_OHLC_FEEDER_ENABLED=false` and the intraday-only
      RVOL fallback resumes.

Configuration (backend/.env):
    KRAKEN_OHLC_FEEDER_ENABLED    "true" to enable (default true)
    KRAKEN_OHLC_POLL_INTERVAL_SEC default: 3600 (1h). Kraken's
                                  public OHLC endpoint has no
                                  per-account rate limit but
                                  responsibly we back off. Daily
                                  bars only close once every 24h
                                  anyway.
    KRAKEN_OHLC_BACKFILL_DAYS     default: 30. On boot the worker
                                  walks the current daily window
                                  and any prior days that are
                                  missing. Idempotent upserts
                                  make this cheap.
    KRAKEN_OHLC_UNIVERSE          Optional CSV override. If unset,
                                  the feeder derives the universe
                                  from `shared_intents` (last 24h
                                  of crypto intents).

Doctrine (data-integrity):
    * Idempotent upsert via `_persist_bar`. Re-runs are cheap.
    * No sensitive data — public endpoint, no auth headers.
    * Bar `source` stamped as `"kraken_pro"` so downstream
      consumers can distinguish from equity feeders. `tf="1d"`.
    * Backwards-compatible: consumers that only read `tf="1d"`
      regardless of `source` (e.g. `_fetch_daily_volume_baseline`)
      pick up crypto RVOL without any code change.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from db import db
from namespaces import SHARED_INTENTS
from shared.crypto.kraken import (
    fetch_ohlc,
    kraken_interval_for_tf,
    to_internal_bar,
    to_kraken_pair,
)
from shared.feeders.feeder_health import record_feeder_health
from shared.technicals import _persist_bar

logger = logging.getLogger(__name__)


PROVIDER = "kraken_pro"
DEFAULT_POLL_INTERVAL_SEC = 3600  # 1h
DEFAULT_BACKFILL_DAYS = 30

# Fallback universe if the intents-derived discovery finds no rows
# (e.g. cold start). Same shape the operator would inject via
# KRAKEN_OHLC_UNIVERSE.
FALLBACK_UNIVERSE = [
    "BTC/USD", "ETH/USD", "SOL/USD", "XRP/USD",
    "ADA/USD", "DOGE/USD", "AVAX/USD", "ATOM/USD",
    "BNB/USD",
]


def _env_bool(key: str, default: bool) -> bool:
    val = os.environ.get(key)
    if val is None or val == "":
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key) or default)
    except (TypeError, ValueError):
        return default


async def _discover_universe() -> list[str]:
    """Discover the active crypto universe.

    Precedence:
      1. `KRAKEN_OHLC_UNIVERSE` env override (CSV).
      2. Crypto symbols with intents in the last 24h.
      3. `FALLBACK_UNIVERSE` (cold start).
    """
    override = os.environ.get("KRAKEN_OHLC_UNIVERSE", "").strip()
    if override:
        return [s.strip().upper() for s in override.split(",") if s.strip()]

    try:
        cutoff = (
            datetime.now(timezone.utc) - timedelta(hours=24)
        ).isoformat()
        syms = await db[SHARED_INTENTS].distinct(
            "symbol",
            {"lane": "crypto", "ingest_ts": {"$gte": cutoff}},
        )
        # Kraken's OHLC endpoint only exposes fiat-quoted pairs of
        # interest — filter to `.../USD` shape to avoid trying
        # unsupported quotes.
        syms = [s for s in syms if isinstance(s, str) and s.endswith("/USD")]
        if syms:
            return sorted(syms)
    except Exception as e:  # noqa: BLE001
        logger.warning("kraken_ohlc: intent-based discovery failed: %r", e)

    return FALLBACK_UNIVERSE


async def _fetch_and_persist_one(symbol: str, backfill_days: int) -> int:
    """Fetch daily OHLC bars for one symbol; persist any that cover
    the last `backfill_days`. Returns count of bars written."""
    kpair = to_kraken_pair(symbol)
    interval = kraken_interval_for_tf("1d")
    # `since` is a UNIX timestamp; Kraken returns bars strictly AFTER
    # that timestamp. Ask for a bit more than backfill_days so the
    # earliest requested bar is included.
    since_ts = int(
        (
            datetime.now(timezone.utc) - timedelta(days=backfill_days + 2)
        ).timestamp()
    )
    try:
        result = await fetch_ohlc(
            kpair, interval_minutes=interval, since=since_ts,
        )
    except Exception as e:  # noqa: BLE001
        await record_feeder_health(
            provider=PROVIDER, endpoint="/0/public/OHLC",
            status_code=None, error_type="request_error",
            message=f"{type(e).__name__}: {str(e)[:400]}",
            context={"symbol": symbol, "pair": kpair, "tf": "1d"},
        )
        return 0

    # Kraken returns `{<altname>: [[t,o,h,l,c,vwap,v,count], ...],
    # "last": <ts>}`. Altname key varies (XBT vs BTC canonicalization),
    # so grab the first non-`last` key.
    rows: list[list] = []
    for k, v in result.items():
        if k == "last":
            continue
        if isinstance(v, list):
            rows = v
            break
    if not rows:
        await record_feeder_health(
            provider=PROVIDER, endpoint="/0/public/OHLC",
            status_code=200, error_type="empty_response",
            message="kraken returned no bars",
            context={"symbol": symbol, "pair": kpair},
        )
        return 0

    written = 0
    for row in rows:
        try:
            bar = to_internal_bar(symbol, "1d", row)
        except (TypeError, ValueError, IndexError) as e:
            logger.warning(
                "kraken_ohlc: bar parse failed for %s: %r", symbol, e,
            )
            continue
        bar["source"] = PROVIDER
        try:
            await _persist_bar(bar)
            written += 1
        except Exception as e:  # noqa: BLE001
            await record_feeder_health(
                provider=PROVIDER, endpoint="_persist_bar",
                status_code=None, error_type="db_error",
                message=f"{type(e).__name__}: {str(e)[:400]}",
                context={"symbol": symbol, "ts": bar.get("ts")},
            )
    return written


async def _tick() -> dict:
    """One poll cycle. Iterates universe → fetches → upserts."""
    backfill_days = _env_int(
        "KRAKEN_OHLC_BACKFILL_DAYS", DEFAULT_BACKFILL_DAYS,
    )
    universe = await _discover_universe()
    total_written = 0
    per_symbol: dict[str, int] = {}
    for sym in universe:
        n = await _fetch_and_persist_one(sym, backfill_days)
        per_symbol[sym] = n
        total_written += n
    # Health OK ping so the coverage-report per_source_health tile
    # shows kraken_pro as `ok` after a clean sweep.
    if total_written > 0:
        await record_feeder_health(
            provider=PROVIDER, endpoint="_tick",
            status_code=200, error_type=None,
            message=f"tick ok: universe={len(universe)} bars_written={total_written}",
            context={"per_symbol": per_symbol},
        )
    return {
        "universe_size": len(universe),
        "bars_written": total_written,
        "per_symbol": per_symbol,
    }


_stop_flag: bool = False
_task: Optional[asyncio.Task] = None


async def _worker_loop() -> None:
    global _stop_flag
    interval = _env_int(
        "KRAKEN_OHLC_POLL_INTERVAL_SEC", DEFAULT_POLL_INTERVAL_SEC,
    )
    logger.info(
        "kraken_ohlc feeder started: interval=%ss backfill_days=%s",
        interval, _env_int("KRAKEN_OHLC_BACKFILL_DAYS", DEFAULT_BACKFILL_DAYS),
    )
    while not _stop_flag:
        try:
            result = await _tick()
            if result.get("bars_written", 0) > 0:
                logger.info(
                    "kraken_ohlc tick: universe=%s bars_written=%s",
                    result["universe_size"], result["bars_written"],
                )
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning("kraken_ohlc tick error: %r", e)
            await record_feeder_health(
                provider=PROVIDER, endpoint="_worker_loop",
                status_code=None, error_type="worker_crash",
                message=str(e)[:500],
            )
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            break


def start_worker_if_enabled() -> None:
    """Spawn the polling task. Idempotent — re-callable on hot reload.
    No-op if disabled by env."""
    global _task, _stop_flag
    if _task is not None and not _task.done():
        return
    enabled = _env_bool("KRAKEN_OHLC_FEEDER_ENABLED", True)
    if not enabled:
        logger.info(
            "kraken_ohlc feeder disabled via "
            "KRAKEN_OHLC_FEEDER_ENABLED=false",
        )
        return
    _stop_flag = False
    _task = asyncio.create_task(_worker_loop(), name="kraken_ohlc_feeder")


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


# ────────────────────── Admin (manual re-trigger) ──────────────────────


async def run_now() -> dict:
    """Manual one-shot invocation — used by the admin re-trigger
    endpoint. Bypasses the sleep loop. Returns the same shape as one
    worker tick."""
    return await _tick()
