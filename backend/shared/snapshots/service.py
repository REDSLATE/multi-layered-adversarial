"""Daily market snapshot capture service.

Public API:
  - `capture_snapshot(label, *, db=None) -> dict`
      Loop the S&P-500 universe, pull each symbol's latest bar from
      `shared_ohlcv_bars` (preferring the `finnhub_equity` source,
      falling back to any other source if absent), compute RVOL,
      persist one row per symbol into `daily_market_snapshots`,
      write one audit row into `daily_snapshot_capture_log`.

  - `wipe_old_snapshots(*, keep_trading_days=5, db=None) -> dict`
      Delete snapshot rows whose `market_day` is older than the
      Nth-most-recent NYSE trading day.

Doctrine:
  - READ-ONLY broker path. The capture worker NEVER calls a broker.
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
# session boundaries so the 5m bar covering the boundary is closed.
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

# Timeframe to capture. 1Day matches the spec used by the brain doc;
# the feeder writes both 5m and 1Day rows.
DEFAULT_SNAPSHOT_TIMEFRAME: str = os.environ.get(
    "MC_SNAPSHOT_BAR_TF", "1Day"
)

# How many NYSE trading days of snapshots to retain. Older rows are
# wiped on the next `open` capture.
SNAPSHOT_RETENTION_TRADING_DAYS: int = int(
    os.environ.get("MC_SNAPSHOT_RETENTION_DAYS", "5")
)

# Concurrency cap for the per-symbol DB lookups. 25 keeps Motor's
# connection pool from being saturated while still completing the
# 500-symbol sweep in well under 30 seconds.
CAPTURE_CONCURRENCY: int = int(os.environ.get("MC_SNAPSHOT_CONCURRENCY", "25"))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _latest_bar_for(
    symbol: str,
    source: str,
    tf: str,
    db,
) -> Dict[str, Any]:
    """Latest bar lookup. Tries the requested source; if missing, falls
    back to ANY other source for the symbol+tf so coverage holes in
    one feed don't show as total blackouts."""
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
            "price": None, "ohlc": None, "asof": None,
            "source": None, "ok": False, "reason": "no_bars_for_symbol",
        }
    close = row.get("c")
    if close is None:
        return {
            "price": None, "ohlc": None, "asof": row.get("ts"),
            "source": used_source, "ok": False,
            "reason": "last_bar_close_missing",
        }
    return {
        "price": float(close),
        "ohlc": {
            "o": row.get("o"), "h": row.get("h"),
            "l": row.get("l"), "c": close, "v": row.get("v"),
        },
        "asof": row.get("ts"),
        "source": used_source,
        "ok": True,
        "reason": None,
    }


async def _build_one(
    symbol: str,
    source: str,
    tf: str,
    db,
) -> Dict[str, Any]:
    """Combine latest-bar + RVOL for one symbol. Doctrine: never
    raises — every failure mode lands as a structured row with
    `ok=False, reason=<...>` so the capture pass can't be tanked by
    a single bad symbol."""
    sym = symbol.upper()
    try:
        latest = await _latest_bar_for(sym, source, tf, db)
        # RVOL uses the requested source explicitly (no fallback) —
        # an RVOL multiple computed across two feeders is noise.
        rv = await compute_relative_volume(
            sym, tf=tf, source=source, db=db,
        )
        return {
            "symbol": sym,
            "price": latest["price"],
            "ohlc": latest["ohlc"],
            "asof": latest["asof"],
            "bar_source": latest["source"],
            "price_ok": latest["ok"],
            "price_reason": latest["reason"],
            "relative_volume": rv["value"],
            "relative_volume_ok": rv["ok"],
            "relative_volume_reason": rv["reason"],
            "basis_bars": rv["basis_bars"],
            "current_v": rv["current_v"],
            "avg_v": rv["avg_v"],
        }
    except Exception as exc:  # noqa: BLE001 — never let one symbol kill capture
        logger.warning(
            "snapshot capture failed for symbol=%s: %s", sym, exc,
        )
        return {
            "symbol": sym,
            "price": None, "ohlc": None, "asof": None,
            "bar_source": None,
            "price_ok": False,
            "price_reason": f"capture_error:{type(exc).__name__}",
            "relative_volume": None,
            "relative_volume_ok": False,
            "relative_volume_reason": f"capture_error:{type(exc).__name__}",
            "basis_bars": 0, "current_v": None, "avg_v": None,
        }


async def capture_snapshot(
    label: str,
    *,
    universe: Optional[Iterable[str]] = None,
    source: str = DEFAULT_SNAPSHOT_SOURCE,
    tf: str = DEFAULT_SNAPSHOT_TIMEFRAME,
    market_day: Optional[date] = None,
    db=None,
) -> Dict[str, Any]:
    """Capture one labeled snapshot of the universe.

    Args:
        label: one of `SNAPSHOT_LABELS` (open|midday|close).
        universe: iterable of tickers (defaults to S&P 500).
        source: bar source to prefer.
        tf: bar timeframe.
        market_day: NYSE trading day this capture represents (default
                    today's market day).
        db: optional Motor db handle for tests.

    Returns:
        {
          "label": str,
          "market_day": "YYYY-MM-DD",
          "captured_at": ISO,
          "universe_size": int,
          "rows_with_price": int,
          "rows_missing_price": int,
          "source": str,
          "tf": str,
        }
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
            return await _build_one(sym, source, tf, db)

    rows = await asyncio.gather(*(_bounded(s) for s in symbols))

    # Persist — one upserted doc per (market_day, label, symbol). The
    # upsert key matches the unique index built by `ensure_indexes`.
    bulk_ops = []
    with_price = 0
    missing_price = 0
    for row in rows:
        if row["price"] is not None:
            with_price += 1
        else:
            missing_price += 1
        doc = {
            "market_day": md_str,
            "label": label,
            "captured_at": captured_at,
            "source": source,
            "tf": tf,
            **row,
        }
        bulk_ops.append(
            {"market_day": md_str, "label": label, "symbol": row["symbol"]},
        )
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
        "rows_with_price": with_price,
        "rows_missing_price": missing_price,
        "source": source,
        "tf": tf,
    }
    await db[DAILY_SNAPSHOT_CAPTURE_LOG].insert_one(dict(summary))
    logger.info(
        "daily_snapshot capture label=%s market_day=%s "
        "with_price=%d missing=%d source=%s tf=%s",
        label, md_str, with_price, missing_price, source, tf,
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
    "DEFAULT_SNAPSHOT_TIMEFRAME",
    "capture_snapshot",
    "wipe_old_snapshots",
    "ensure_indexes",
)
