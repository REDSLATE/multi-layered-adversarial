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
DEFAULT_POLL_INTERVAL_SEC = 3600  # 1h — daily bars only close 1×/day
DEFAULT_BACKFILL_DAYS = 30

# ── Intraday (5m) extension, 2026-07 parity work ──
# Camino's pulse migration needs INTRADAY crypto bars to build a
# meaningful market snapshot; the daily bars alone force
# `session_features.trend_score=None` and reduce Camino to
# doctrine-only reads. Kraken's public OHLC endpoint returns 5m
# bars unauthenticated (verified 2026-07-11), so we run a second,
# faster loop alongside the daily poller. The two loops share
# per-symbol upsert semantics via `_persist_bar`; a symbol writing
# both `tf=1d` and `tf=5m` is exactly the design.
#
# 5m bars close every 5 minutes → 60s poll cadence lands each bar
# well within a minute of close. Kraken's 5m response is naturally
# window-limited to a few hundred rows per call so a 3h backfill
# is generous and cheap.
DEFAULT_INTRADAY_POLL_INTERVAL_SEC = 60      # 1 min
DEFAULT_INTRADAY_BACKFILL_HOURS = 3

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


MAX_UNIVERSE = 60  # rate-limit guard on the merged universe


async def _recent_intent_symbols(hours: int = 24) -> list[str]:
    """Tradable-pair coverage repair (2026-07-28): any /USD pair a
    brain actually emitted an intent for in the window. Unioned into
    every discovery result so actively traded pairs (e.g. ETH) never
    lack 5m bars and fall back to NO_DATA in snapshot enrichment."""
    try:
        cutoff = (
            datetime.now(timezone.utc) - timedelta(hours=hours)
        ).isoformat()
        syms = await db[SHARED_INTENTS].distinct(
            "symbol",
            {"lane": "crypto", "ingest_ts": {"$gte": cutoff}},
        )
        return sorted(
            s for s in syms if isinstance(s, str) and s.endswith("/USD")
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("kraken_ohlc: intent-symbol discovery failed: %r", e)
        return []


def _cap_universe(syms: set[str]) -> list[str]:
    return sorted(syms)[:MAX_UNIVERSE]


async def _discover_universe() -> list[str]:
    """Discover the active crypto universe.

    Precedence:
      1. `KRAKEN_OHLC_UNIVERSE` env override (CSV).
      2. Operator-curated `patterns_universe` collection
         (lane=crypto, active=true). This is the canonical
         20-symbol crypto universe the operator maintains via
         `/api/admin/patterns/universe`.
      3. Legacy fallback: crypto symbols with intents in the last
         24h. Kept as a graceful degradation for pre-2026-02
         installs; `patterns_universe` supersedes it.
      4. `FALLBACK_UNIVERSE` (cold start, no admin curation).
    """
    override = os.environ.get("KRAKEN_OHLC_UNIVERSE", "").strip()
    if override:
        return [s.strip().upper() for s in override.split(",") if s.strip()]

    # ── Primary: live_universe (broker-driven, 15min refresh) ──
    # 2026-07-15 (iter-30 P4): the discovery hierarchy now has
    # `live_universe` at the top — it's rebuilt every 15min from
    # Kraken's 24h movers + high-liquidity pairs. `patterns_universe`
    # remains as a fallback and operator-pin merge layer.
    try:
        from shared.universe.live_universe import read_universe  # noqa: WPS433
        doc = await read_universe("crypto")
        if doc:
            syms = sorted({
                (s.get("canonical_symbol") or "").upper()
                for s in (doc.get("symbols") or [])
                if s.get("canonical_symbol") and s.get("tradable", True)
            })
            if syms:
                return syms
    except Exception as e:  # noqa: BLE001
        logger.warning("kraken_ohlc: live_universe discovery failed: %r", e)

    # ── Fallback: operator-curated `patterns_universe` ──
    try:
        cursor = db["patterns_universe"].find(
            {"lane": "crypto", "active": True},
            {"symbol": 1, "_id": 0},
        ).max_time_ms(2000).limit(200)
        docs = await cursor.to_list(200)
        syms = sorted({(d.get("symbol") or "").upper() for d in docs if d.get("symbol")})
        if syms:
            return _cap_universe(
                set(syms) | set(await _recent_intent_symbols())
            )
    except Exception as e:  # noqa: BLE001
        logger.warning("kraken_ohlc: patterns_universe discovery failed: %r", e)

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


async def _fetch_and_persist_one(
    symbol: str, backfill_days: float, tf: str = "1d",
) -> int:
    """Fetch OHLC bars for one symbol at `tf`; persist any that
    cover the last `backfill_days` (days) or an equivalent window
    for intraday tfs. Returns count of bars written.

    Note on the "since" cursor: Kraken's OHLC endpoint returns
    bars strictly AFTER the `since` timestamp. We overshoot by a
    small margin so the earliest requested bar is guaranteed
    included; the idempotent upsert on `(source, symbol, tf, ts)`
    makes duplicates a no-op.
    """
    kpair = to_kraken_pair(symbol)
    interval = kraken_interval_for_tf(tf)
    # For intraday, `backfill_days` is interpreted as a fractional
    # day. Callers pass `hours / 24.0` for a 3h backfill etc.
    since_ts = int(
        (
            datetime.now(timezone.utc)
            - timedelta(days=backfill_days + (2 if tf == "1d" else 0))
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
            context={"symbol": symbol, "pair": kpair, "tf": tf},
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
            context={"symbol": symbol, "pair": kpair, "tf": tf},
        )
        return 0

    written = 0
    for row in rows:
        try:
            bar = to_internal_bar(symbol, tf, row)
        except (TypeError, ValueError, IndexError) as e:
            logger.warning(
                "kraken_ohlc: bar parse failed for %s tf=%s: %r",
                symbol, tf, e,
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
                context={"symbol": symbol, "tf": tf, "ts": bar.get("ts")},
            )
    return written


async def _tick() -> dict:
    """One DAILY poll cycle. Iterates universe → fetches → upserts."""
    backfill_days = _env_int(
        "KRAKEN_OHLC_BACKFILL_DAYS", DEFAULT_BACKFILL_DAYS,
    )
    universe = await _discover_universe()
    total_written = 0
    per_symbol: dict[str, int] = {}
    for sym in universe:
        n = await _fetch_and_persist_one(sym, backfill_days, tf="1d")
        per_symbol[sym] = n
        total_written += n
    # Health OK ping so the coverage-report per_source_health tile
    # shows kraken_pro as `ok` after a clean sweep.
    if total_written > 0:
        await record_feeder_health(
            provider=PROVIDER, endpoint="_tick",
            status_code=200, error_type=None,
            message=f"tick ok: universe={len(universe)} bars_written={total_written}",
            context={"per_symbol": per_symbol, "tf": "1d"},
        )
    return {
        "universe_size": len(universe),
        "bars_written": total_written,
        "per_symbol": per_symbol,
        "tf": "1d",
    }


async def _tick_intraday() -> dict:
    """One 5m poll cycle. Same shape as `_tick()` but writes
    `tf=5m` with a short (default 3h) backfill window.

    2026-07 parity work — brought online alongside the daily
    poll so the crypto pulse snapshot can build a real 20-bar
    hot-branch window without falling back to daily-derived
    features. See module header note under "Intraday (5m)
    extension".
    """
    backfill_hours = _env_int(
        "KRAKEN_OHLC_INTRADAY_BACKFILL_HOURS",
        DEFAULT_INTRADAY_BACKFILL_HOURS,
    )
    # `_fetch_and_persist_one` takes `backfill_days` as a float
    # under the hood; converting hours→days here keeps the
    # signature clean for the daily caller.
    backfill_days = max(0.05, backfill_hours / 24.0)   # min ~72m
    universe = await _discover_universe()
    total_written = 0
    per_symbol: dict[str, int] = {}
    for sym in universe:
        n = await _fetch_and_persist_one(sym, backfill_days, tf="5m")
        per_symbol[sym] = n
        total_written += n
    if total_written > 0:
        await record_feeder_health(
            provider=PROVIDER, endpoint="_tick_intraday",
            status_code=200, error_type=None,
            message=f"5m tick ok: universe={len(universe)} bars_written={total_written}",
            context={"per_symbol": per_symbol, "tf": "5m"},
        )
    return {
        "universe_size": len(universe),
        "bars_written": total_written,
        "per_symbol": per_symbol,
        "tf": "5m",
    }


_stop_flag: bool = False
_task: Optional[asyncio.Task] = None
_intraday_task: Optional[asyncio.Task] = None


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


async def _intraday_worker_loop() -> None:
    """Parallel loop dedicated to `tf=5m` bar polling.

    Runs on a separate task from the daily loop because the two
    cadences differ by ~60× (60s vs 3600s) — sharing one loop
    would either starve the daily poll or hammer intraday too
    slowly. The two loops share the same universe + fail-mode
    plumbing but have independent stop flags via _stop_flag
    below.

    2026-07 parity work (iter-27) — brought online so Camino's
    pulse migration can build a real 20-bar hot-branch snapshot
    for crypto symbols. Without this, the pulse falls back to
    daily bars and Camino emits `INSUFFICIENT_DATA`
    (`trend_score` missing) on the crypto lane.
    """
    global _stop_flag
    interval = _env_int(
        "KRAKEN_OHLC_INTRADAY_POLL_INTERVAL_SEC",
        DEFAULT_INTRADAY_POLL_INTERVAL_SEC,
    )
    backfill_hours = _env_int(
        "KRAKEN_OHLC_INTRADAY_BACKFILL_HOURS",
        DEFAULT_INTRADAY_BACKFILL_HOURS,
    )
    logger.info(
        "kraken_ohlc intraday feeder started: "
        "interval=%ss backfill_hours=%s tf=5m",
        interval, backfill_hours,
    )
    while not _stop_flag:
        try:
            result = await _tick_intraday()
            if result.get("bars_written", 0) > 0:
                logger.info(
                    "kraken_ohlc 5m tick: universe=%s bars_written=%s",
                    result["universe_size"], result["bars_written"],
                )
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning("kraken_ohlc 5m tick error: %r", e)
            await record_feeder_health(
                provider=PROVIDER, endpoint="_intraday_worker_loop",
                status_code=None, error_type="worker_crash",
                message=str(e)[:500],
            )
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            break


def start_worker_if_enabled() -> None:
    """Spawn the polling task(s). Idempotent — re-callable on hot
    reload. No-op if disabled by env.

    Starts up to TWO tasks:
      * Daily poll (KRAKEN_OHLC_FEEDER_ENABLED, default true)
      * Intraday 5m poll (KRAKEN_OHLC_INTRADAY_ENABLED, default true)
    """
    global _task, _intraday_task, _stop_flag
    enabled = _env_bool("KRAKEN_OHLC_FEEDER_ENABLED", True)
    if not enabled:
        logger.info(
            "kraken_ohlc feeder disabled via "
            "KRAKEN_OHLC_FEEDER_ENABLED=false",
        )
        return
    _stop_flag = False
    if _task is None or _task.done():
        _task = asyncio.create_task(_worker_loop(), name="kraken_ohlc_feeder")
    # Intraday runs beside the daily poll; independent enable flag
    # so it can be turned off without disabling the daily baseline
    # supplier that RVOL still depends on.
    intraday_enabled = _env_bool("KRAKEN_OHLC_INTRADAY_ENABLED", True)
    if not intraday_enabled:
        logger.info(
            "kraken_ohlc intraday (5m) feeder disabled via "
            "KRAKEN_OHLC_INTRADAY_ENABLED=false",
        )
        return
    if _intraday_task is None or _intraday_task.done():
        _intraday_task = asyncio.create_task(
            _intraday_worker_loop(), name="kraken_ohlc_intraday_feeder",
        )


async def stop_worker() -> None:
    global _task, _intraday_task, _stop_flag
    _stop_flag = True
    for t in (_task, _intraday_task):
        if t is not None and not t.done():
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
    _task = None
    _intraday_task = None


# ────────────────────── Admin (manual re-trigger) ──────────────────────


async def run_now() -> dict:
    """Manual one-shot invocation — used by the admin re-trigger
    endpoint. Bypasses the sleep loop. Returns the same shape as one
    worker tick."""
    return await _tick()
