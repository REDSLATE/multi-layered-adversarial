"""Daily market snapshot capture service.

Captures TWO timeframes per snapshot row, per spec (2026-05-31 ask
"include OHLCV — both intraday + daily"):

  - `intraday` (default `5m`): the bar that just closed at the
    capture moment. Open=09:30 bar, Midday=12:25 bar, Close=15:55
    bar. Lets brains see intraday volatility / RVOL.

  - `daily`   (default `1d`):  the most-recent completed daily bar.
    On `open` + `midday` this is yesterday's daily; on `close` it's
    today's.

Public API:
  - `capture_snapshot(label, *, db=None) -> dict`
      Loop the S&P-500 universe. For each symbol fan out FOUR DB
      queries in parallel (latest 5m bar, latest 1d bar, RVOL on 5m,
      RVOL on 1d), assemble one row with nested `intraday` + `daily`
      blocks, upsert into `daily_market_snapshots`.

  - `wipe_old_snapshots(*, keep_trading_days=5, db=None) -> dict`
      Delete snapshot rows whose `market_day` is older than the
      Nth-most-recent NYSE trading day.

Doctrine:
  - READ-ONLY broker path. Never calls a broker quote endpoint.
    Symbols without bars get `price=None, price_reason="no_bars_for_symbol"`
    so coverage gaps are auditable.
  - Idempotent. Re-running a capture for the same (market_day, label)
    upserts each row; safe to re-fire after a crash.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import date, datetime, timezone
from typing import Any, Dict, Iterable, Optional

from db import db as _default_db
from namespaces import (
    DAILY_MARKET_SNAPSHOTS,
    DAILY_SNAPSHOT_CAPTURE_LOG,
    SHARED_OHLCV_BARS,
)
from shared.market_data.feature_service import compute_relative_volume
from shared.snapshots.nyse_calendar import (
    market_day_today,
    previous_n_trading_days,
)
from shared.snapshots.sp500_universe import SP500_TICKERS


logger = logging.getLogger("risedual.daily_snapshot")


# Three canonical labels. The worker fires one capture per label per
# trading day. Order is meaningful for the operator UI; do not
# alphabetize.
SNAPSHOT_LABELS: tuple[str, ...] = ("open", "midday", "close")

# Per-label capture times in US/Eastern (24h). 5-minute offsets from
# session boundaries so the bar covering the boundary is closed.
SNAPSHOT_TIMES_ET: dict[str, tuple[int, int]] = {
    "open":   (9, 35),
    "midday": (12, 30),
    "close":  (16, 5),
}

# Preferred bar source. The Finnhub equity feeder writes here on
# every poll. If the operator changes feeders, this env-var lets the
# worker follow without code changes.
DEFAULT_SNAPSHOT_SOURCE: str = os.environ.get(
    "MC_SNAPSHOT_BAR_SOURCE", "finnhub_equity"
)

# Two timeframes captured per row. Must match the values the Finnhub
# equity feeder writes into `shared_ohlcv_bars` (see
# `shared/feeders/finnhub_equity.py::_RES_TO_TF`).
INTRADAY_TIMEFRAME: str = os.environ.get("MC_SNAPSHOT_INTRADAY_TF", "5m")
DAILY_TIMEFRAME: str = os.environ.get("MC_SNAPSHOT_DAILY_TF", "1d")

# How many NYSE trading days of snapshots to retain. Older rows are
# wiped on the next `open` capture.
SNAPSHOT_RETENTION_TRADING_DAYS: int = int(
    os.environ.get("MC_SNAPSHOT_RETENTION_DAYS", "5")
)

# Concurrency cap for the per-symbol DB lookups. 25 keeps Motor's
# connection pool from being saturated while still completing the
# 500-symbol × 4-query sweep in well under 30 seconds.
CAPTURE_CONCURRENCY: int = int(os.environ.get("MC_SNAPSHOT_CONCURRENCY", "25"))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _latest_bar_for(
    symbol: str,
    source: str,
    tf: str,
    db,
) -> Dict[str, Any]:
    """Latest bar lookup for one (symbol, tf). Tries the requested
    source first; falls back to ANY other source on the same tf so
    coverage holes in one feed don't appear as total blackouts."""
    row = await db[SHARED_OHLCV_BARS].find_one(
        {"source": source, "symbol": symbol, "tf": tf},
        {"_id": 0, "o": 1, "h": 1, "l": 1, "c": 1, "v": 1, "ts": 1, "source": 1},
        sort=[("ts", -1)],
    )
    used_source = source
    if not row:
        row = await db[SHARED_OHLCV_BARS].find_one(
            {"symbol": symbol, "tf": tf},
            {"_id": 0, "o": 1, "h": 1, "l": 1, "c": 1, "v": 1, "ts": 1, "source": 1},
            sort=[("ts", -1)],
        )
        used_source = row.get("source") if row else None
    if not row:
        return {
            "tf": tf,
            "price": None, "ohlc": None, "asof": None,
            "bar_source": None, "price_ok": False,
            "price_reason": "no_bars_for_symbol",
            "relative_volume": None,
            "relative_volume_ok": False,
            "relative_volume_reason": "no_bars_for_symbol",
            "basis_bars": 0,
            "current_v": None, "avg_v": None,
        }
    close = row.get("c")
    if close is None:
        return {
            "tf": tf,
            "price": None, "ohlc": None, "asof": row.get("ts"),
            "bar_source": used_source, "price_ok": False,
            "price_reason": "last_bar_close_missing",
            "relative_volume": None,
            "relative_volume_ok": False,
            "relative_volume_reason": "last_bar_close_missing",
            "basis_bars": 0,
            "current_v": None, "avg_v": None,
        }
    return {
        "tf": tf,
        "price": float(close),
        "ohlc": {
            "o": row.get("o"), "h": row.get("h"),
            "l": row.get("l"), "c": close, "v": row.get("v"),
        },
        "asof": row.get("ts"),
        "bar_source": used_source,
        "price_ok": True,
        "price_reason": None,
        # RVOL is filled in by _build_one (separate query).
        "relative_volume": None,
        "relative_volume_ok": False,
        "relative_volume_reason": "pending",
        "basis_bars": 0,
        "current_v": None,
        "avg_v": None,
    }


async def _tf_block(
    symbol: str,
    source: str,
    tf: str,
    db,
) -> Dict[str, Any]:
    """One timeframe's full block: latest bar + RVOL. Runs the two
    DB hits in parallel so a 502-symbol capture stays fast."""
    bar_task = asyncio.create_task(_latest_bar_for(symbol, source, tf, db))
    rv_task = asyncio.create_task(
        compute_relative_volume(symbol, tf=tf, source=source, db=db),
    )
    bar, rv = await asyncio.gather(bar_task, rv_task)
    # Splice RVOL into the bar block.
    bar["relative_volume"] = rv["value"]
    bar["relative_volume_ok"] = rv["ok"]
    bar["relative_volume_reason"] = rv["reason"]
    bar["basis_bars"] = rv["basis_bars"]
    bar["current_v"] = rv["current_v"]
    bar["avg_v"] = rv["avg_v"]
    return bar


async def _build_one(
    symbol: str,
    source: str,
    intraday_tf: str,
    daily_tf: str,
    db,
) -> Dict[str, Any]:
    """Build one symbol's snapshot row. Doctrine: never raises —
    every failure mode lands as a structured row with the affected
    timeframe block carrying `*_ok=False, *_reason=<...>` so the
    capture pass can't be tanked by a single bad symbol."""
    sym = symbol.upper()
    try:
        intraday, daily = await asyncio.gather(
            _tf_block(sym, source, intraday_tf, db),
            _tf_block(sym, source, daily_tf, db),
        )
        return {"symbol": sym, "intraday": intraday, "daily": daily}
    except Exception as exc:  # noqa: BLE001 — never let one symbol kill capture
        logger.warning(
            "snapshot capture failed for symbol=%s: %s", sym, exc,
        )
        reason = f"capture_error:{type(exc).__name__}"

        def _err_block(tf: str) -> Dict[str, Any]:
            return {
                "tf": tf,
                "price": None, "ohlc": None, "asof": None,
                "bar_source": None, "price_ok": False,
                "price_reason": reason,
                "relative_volume": None,
                "relative_volume_ok": False,
                "relative_volume_reason": reason,
                "basis_bars": 0, "current_v": None, "avg_v": None,
            }

        return {
            "symbol": sym,
            "intraday": _err_block(intraday_tf),
            "daily": _err_block(daily_tf),
        }


async def capture_snapshot(
    label: str,
    *,
    universe: Optional[Iterable[str]] = None,
    source: str = DEFAULT_SNAPSHOT_SOURCE,
    intraday_tf: str = INTRADAY_TIMEFRAME,
    daily_tf: str = DAILY_TIMEFRAME,
    market_day: Optional[date] = None,
    db=None,
) -> Dict[str, Any]:
    """Capture one labeled snapshot of the universe with BOTH 5m and
    1d OHLCV per symbol.

    Args:
        label: one of `SNAPSHOT_LABELS` (open|midday|close).
        universe: iterable of tickers (defaults to S&P 500).
        source: preferred bar source.
        intraday_tf: timeframe for the intraday block (default 5m).
        daily_tf: timeframe for the daily block (default 1d).
        market_day: NYSE trading day this capture represents (default
                    today's market day).
        db: optional Motor db handle for tests.

    Returns:
        summary dict (counts for both timeframes' coverage).
    """
    if label not in SNAPSHOT_LABELS:
        raise ValueError(
            f"label must be one of {SNAPSHOT_LABELS}, got {label!r}"
        )
    if db is None:
        db = _default_db
    if market_day is None:
        market_day = market_day_today()
    if universe is None:
        universe = SP500_TICKERS

    symbols = [s.upper() for s in universe]
    md_str = market_day.isoformat()
    captured_at = _now_iso()

    sem = asyncio.Semaphore(CAPTURE_CONCURRENCY)

    async def _bounded(sym: str):
        async with sem:
            return await _build_one(sym, source, intraday_tf, daily_tf, db)

    rows = await asyncio.gather(*(_bounded(s) for s in symbols))

    intraday_with_price = 0
    daily_with_price = 0
    for row in rows:
        if row["intraday"]["price"] is not None:
            intraday_with_price += 1
        if row["daily"]["price"] is not None:
            daily_with_price += 1
        doc = {
            "market_day": md_str,
            "label": label,
            "captured_at": captured_at,
            "source": source,
            "intraday_tf": intraday_tf,
            "daily_tf": daily_tf,
            "symbol": row["symbol"],
            "intraday": row["intraday"],
            "daily": row["daily"],
        }
        await db[DAILY_MARKET_SNAPSHOTS].update_one(
            {
                "market_day": md_str,
                "label": label,
                "symbol": row["symbol"],
            },
            {"$set": doc},
            upsert=True,
        )

    summary = {
        "label": label,
        "market_day": md_str,
        "captured_at": captured_at,
        "universe_size": len(symbols),
        "intraday_tf": intraday_tf,
        "intraday_rows_with_price": intraday_with_price,
        "intraday_rows_missing_price": len(symbols) - intraday_with_price,
        "daily_tf": daily_tf,
        "daily_rows_with_price": daily_with_price,
        "daily_rows_missing_price": len(symbols) - daily_with_price,
        "source": source,
    }
    await db[DAILY_SNAPSHOT_CAPTURE_LOG].insert_one(dict(summary))
    logger.info(
        "daily_snapshot capture label=%s market_day=%s "
        "intraday_with_price=%d daily_with_price=%d source=%s "
        "intraday_tf=%s daily_tf=%s",
        label, md_str,
        intraday_with_price, daily_with_price,
        source, intraday_tf, daily_tf,
    )
    return summary


async def wipe_old_snapshots(
    *,
    keep_trading_days: int = SNAPSHOT_RETENTION_TRADING_DAYS,
    anchor: Optional[date] = None,
    db=None,
) -> Dict[str, Any]:
    """Delete snapshot rows older than the Nth-most-recent NYSE
    trading day. Returns a summary with how many rows were removed.

    Idempotent. Safe to call multiple times per day.
    """
    if db is None:
        db = _default_db
    if anchor is None:
        anchor = market_day_today()

    keep_dates = previous_n_trading_days(anchor, keep_trading_days)
    keep_strs = [d.isoformat() for d in keep_dates]

    snap_result = await db[DAILY_MARKET_SNAPSHOTS].delete_many(
        {"market_day": {"$nin": keep_strs}},
    )
    log_result = await db[DAILY_SNAPSHOT_CAPTURE_LOG].delete_many(
        {"market_day": {"$nin": keep_strs}},
    )
    summary = {
        "wiped_at": _now_iso(),
        "kept_market_days": keep_strs,
        "snapshot_rows_deleted": snap_result.deleted_count,
        "capture_log_rows_deleted": log_result.deleted_count,
    }
    logger.info(
        "daily_snapshot wipe kept=%s deleted_rows=%d "
        "deleted_log_rows=%d",
        keep_strs,
        snap_result.deleted_count,
        log_result.deleted_count,
    )
    return summary


async def ensure_indexes(db=None) -> None:
    """Create the indexes the snapshot system needs. Idempotent;
    Motor's `create_index` is a no-op when the index already exists."""
    if db is None:
        db = _default_db
    await db[DAILY_MARKET_SNAPSHOTS].create_index(
        [("market_day", -1), ("label", 1), ("symbol", 1)],
        unique=True,
        name="daily_market_snapshots_key",
    )
    await db[DAILY_MARKET_SNAPSHOTS].create_index(
        [("market_day", -1), ("label", 1)],
        name="daily_market_snapshots_by_day",
    )
    await db[DAILY_SNAPSHOT_CAPTURE_LOG].create_index(
        [("market_day", -1), ("label", 1)],
        name="daily_snapshot_capture_log_by_day",
    )


__all__ = (
    "SNAPSHOT_LABELS",
    "SNAPSHOT_TIMES_ET",
    "SNAPSHOT_RETENTION_TRADING_DAYS",
    "DEFAULT_SNAPSHOT_SOURCE",
    "INTRADAY_TIMEFRAME",
    "DAILY_TIMEFRAME",
    "capture_snapshot",
    "wipe_old_snapshots",
    "ensure_indexes",
)
