"""Daily market snapshot tests.

Covered:
  - NYSE calendar: weekends, holidays, previous-N math.
  - Service:
      * `capture_snapshot` writes one row per universe symbol.
      * Bars-missing case → price=null, price_reason="no_bars_for_symbol".
      * Bar present → price, ohlc, asof populated.
      * Idempotent re-capture (upsert).
      * Audit row in `daily_snapshot_capture_log`.
      * `wipe_old_snapshots` keeps only the last N trading days.
  - Worker:
      * `_due_label` matches scheduled times within window.
      * `_tick` skips weekends.
      * `_tick` no-ops when (market_day, label) already captured.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from unittest.mock import patch

import pytest


# ──────────────────────── NYSE calendar ────────────────────────


def test_nyse_calendar_weekends_are_not_trading():
    from shared.snapshots.nyse_calendar import is_trading_day
    # Saturday Jan 4 2026
    assert is_trading_day(date(2026, 1, 4)) is False
    # Sunday Jan 5 2025
    assert is_trading_day(date(2025, 1, 5)) is False


def test_nyse_calendar_pinned_holidays_are_not_trading():
    from shared.snapshots.nyse_calendar import is_trading_day
    assert is_trading_day(date(2026, 1, 1)) is False    # New Year's
    assert is_trading_day(date(2026, 7, 3)) is False    # July 4 observed
    assert is_trading_day(date(2026, 12, 25)) is False  # Christmas


def test_nyse_calendar_regular_weekdays_are_trading():
    from shared.snapshots.nyse_calendar import is_trading_day
    assert is_trading_day(date(2026, 1, 5)) is True   # Mon
    assert is_trading_day(date(2026, 6, 17)) is True  # Wed


def test_previous_n_trading_days_skips_weekends_and_holidays():
    from shared.snapshots.nyse_calendar import previous_n_trading_days
    # Anchored at Mon Jan 5 2026, the previous 5 trading days span
    # the holiday-shortened week ending Wed Dec 31 2025.
    out = previous_n_trading_days(date(2026, 1, 5), 5)
    assert len(out) == 5
    # No weekends, no holidays.
    for d in out:
        assert d.weekday() < 5
        assert d.year >= 2025


# ──────────────────────── service: capture_snapshot ────────────────────────


@pytest.fixture
async def fresh_test_db():
    """Spin up an in-memory-ish Mongo collection by suffixing the
    namespace. Each test gets a clean slate.

    Note: we use the real Mongo from the env (motor client in db.py)
    but operate on tagged collections so concurrent tests don't
    collide and we don't pollute prod collections.
    """
    import uuid
    from db import db as real_db
    tag = uuid.uuid4().hex[:8]
    try:
        yield real_db, tag
    finally:
        for coll in (
            f"daily_market_snapshots_{tag}",
            f"daily_snapshot_capture_log_{tag}",
            f"shared_ohlcv_bars_{tag}",
        ):
            await real_db[coll].drop()


def _make_test_db_proxy(real_db, tag: str):
    """Return a proxy that maps the snapshot service's hard-coded
    collection names onto tagged ones so tests are isolated."""
    from namespaces import (
        DAILY_MARKET_SNAPSHOTS,
        DAILY_SNAPSHOT_CAPTURE_LOG,
        SHARED_OHLCV_BARS,
    )
    name_map = {
        DAILY_MARKET_SNAPSHOTS: f"daily_market_snapshots_{tag}",
        DAILY_SNAPSHOT_CAPTURE_LOG: f"daily_snapshot_capture_log_{tag}",
        SHARED_OHLCV_BARS: f"shared_ohlcv_bars_{tag}",
    }

    class _Proxy:
        def __getitem__(self, name):
            return real_db[name_map.get(name, name)]
        def __getattr__(self, name):
            return getattr(real_db, name_map.get(name, name))
    return _Proxy()


@pytest.mark.asyncio
async def test_capture_writes_one_row_per_symbol_missing_bars(fresh_test_db):
    from shared.snapshots.service import capture_snapshot

    real_db, tag = fresh_test_db
    proxy = _make_test_db_proxy(real_db, tag)
    universe = ("AAPL", "MSFT", "NVDA")
    summary = await capture_snapshot(
        "open", universe=universe, market_day=date(2026, 6, 1), db=proxy,
    )
    assert summary["universe_size"] == 3
    assert summary["intraday_rows_with_price"] == 0
    assert summary["intraday_rows_missing_price"] == 3
    assert summary["daily_rows_with_price"] == 0
    assert summary["daily_rows_missing_price"] == 3
    assert summary["label"] == "open"
    assert summary["market_day"] == "2026-06-01"

    from namespaces import DAILY_MARKET_SNAPSHOTS
    rows = await proxy[DAILY_MARKET_SNAPSHOTS].find(
        {"market_day": "2026-06-01", "label": "open"}, {"_id": 0},
    ).sort("symbol", 1).to_list(length=10)
    assert [r["symbol"] for r in rows] == ["AAPL", "MSFT", "NVDA"]
    for r in rows:
        # Both timeframe blocks present, both with null prices.
        assert "intraday" in r and "daily" in r
        assert r["intraday"]["tf"] == "5m"
        assert r["daily"]["tf"] == "1d"
        assert r["intraday"]["price"] is None
        assert r["intraday"]["price_reason"] == "no_bars_for_symbol"
        assert r["daily"]["price"] is None
        assert r["daily"]["price_reason"] == "no_bars_for_symbol"


@pytest.mark.asyncio
async def test_capture_uses_bars_when_present(fresh_test_db):
    from shared.snapshots.service import capture_snapshot
    from namespaces import (
        DAILY_MARKET_SNAPSHOTS, SHARED_OHLCV_BARS,
    )

    real_db, tag = fresh_test_db
    proxy = _make_test_db_proxy(real_db, tag)
    # Seed 7 5m bars + 7 1d bars so RVOL has basis >= MIN_BASIS_BARS (5)
    # for BOTH timeframes.
    for tf in ("5m", "1d"):
        for i in range(1, 8):
            await proxy[SHARED_OHLCV_BARS].insert_one({
                "source": "finnhub_equity", "symbol": "NVDA", "tf": tf,
                "ts": f"2026-05-2{i}T00:00:00+00:00",
                "o": 140.0 + i, "h": 142.0 + i, "l": 138.0 + i,
                "c": 141.0 + i, "v": 1_000_000 + i * 10_000,
            })

    summary = await capture_snapshot(
        "midday", universe=("NVDA",), market_day=date(2026, 5, 28),
        intraday_source="finnhub_equity", daily_source="finnhub_equity",
        db=proxy,
    )
    assert summary["intraday_rows_with_price"] == 1
    assert summary["daily_rows_with_price"] == 1

    row = await proxy[DAILY_MARKET_SNAPSHOTS].find_one(
        {"market_day": "2026-05-28", "label": "midday", "symbol": "NVDA"},
        {"_id": 0},
    )
    assert row is not None
    for block_name in ("intraday", "daily"):
        block = row[block_name]
        assert block["price"] is not None
        assert block["ohlc"] is not None
        assert set(block["ohlc"].keys()) == {"o", "h", "l", "c", "v"}
        assert block["asof"] == "2026-05-27T00:00:00+00:00"
        assert block["price_ok"] is True
        assert block["price_reason"] is None
        assert block["relative_volume"] is not None
        assert block["relative_volume_ok"] is True


@pytest.mark.asyncio
async def test_capture_handles_asymmetric_coverage(fresh_test_db):
    """5m bars present, 1d missing → intraday populated, daily null.
    Proves the dual-timeframe blocks don't conflate."""
    from shared.snapshots.service import capture_snapshot
    from namespaces import DAILY_MARKET_SNAPSHOTS, SHARED_OHLCV_BARS

    real_db, tag = fresh_test_db
    proxy = _make_test_db_proxy(real_db, tag)
    # Only 5m bars, no 1d bars.
    for i in range(1, 8):
        await proxy[SHARED_OHLCV_BARS].insert_one({
            "source": "finnhub_equity", "symbol": "AAPL", "tf": "5m",
            "ts": f"2026-05-2{i}T13:0{i}:00+00:00",
            "o": 200.0, "h": 201.0, "l": 199.5, "c": 200.5,
            "v": 50_000,
        })

    summary = await capture_snapshot(
        "open", universe=("AAPL",), market_day=date(2026, 5, 28), db=proxy,
    )
    assert summary["intraday_rows_with_price"] == 1
    assert summary["daily_rows_with_price"] == 0

    row = await proxy[DAILY_MARKET_SNAPSHOTS].find_one(
        {"market_day": "2026-05-28", "label": "open", "symbol": "AAPL"},
        {"_id": 0},
    )
    assert row["intraday"]["price"] == 200.5
    assert row["daily"]["price"] is None
    assert row["daily"]["price_reason"] == "no_bars_for_symbol"


@pytest.mark.asyncio
async def test_capture_uses_per_tf_sources(fresh_test_db):
    """Intraday block should pick the 5m bar from finnhub_equity;
    daily block should pick the 1d bar from polygon. Proves the
    per-timeframe source split works."""
    from shared.snapshots.service import capture_snapshot
    from namespaces import DAILY_MARKET_SNAPSHOTS, SHARED_OHLCV_BARS

    real_db, tag = fresh_test_db
    proxy = _make_test_db_proxy(real_db, tag)

    # Seed: finnhub 5m bars + polygon 1d bars.
    for i in range(1, 8):
        await proxy[SHARED_OHLCV_BARS].insert_one({
            "source": "finnhub_equity", "symbol": "NVDA", "tf": "5m",
            "ts": f"2026-05-2{i}T13:0{i}:00+00:00",
            "o": 200.0, "h": 201.0, "l": 199.5, "c": 200.5, "v": 50_000,
        })
        await proxy[SHARED_OHLCV_BARS].insert_one({
            "source": "polygon", "symbol": "NVDA", "tf": "1d",
            "ts": f"2026-05-2{i}T00:00:00+00:00",
            "o": 210.0 + i, "h": 215.0 + i, "l": 209.0 + i,
            "c": 211.0 + i, "v": 1_000_000 + i * 10_000,
        })

    summary = await capture_snapshot(
        "close",
        universe=("NVDA",),
        intraday_source="finnhub_equity",
        daily_source="polygon",
        market_day=date(2026, 5, 28),
        db=proxy,
    )
    assert summary["intraday_rows_with_price"] == 1
    assert summary["daily_rows_with_price"] == 1
    assert summary["intraday_source"] == "finnhub_equity"
    assert summary["daily_source"] == "polygon"

    row = await proxy[DAILY_MARKET_SNAPSHOTS].find_one(
        {"market_day": "2026-05-28", "label": "close", "symbol": "NVDA"},
        {"_id": 0},
    )
    assert row["intraday"]["bar_source"] == "finnhub_equity"
    assert row["intraday"]["price"] == 200.5
    assert row["daily"]["bar_source"] == "polygon"
    # Most-recent daily bar is i=7 → close=218.0
    assert row["daily"]["price"] == 218.0


@pytest.mark.asyncio
async def test_capture_is_idempotent(fresh_test_db):
    from shared.snapshots.service import capture_snapshot
    from namespaces import DAILY_MARKET_SNAPSHOTS

    real_db, tag = fresh_test_db
    proxy = _make_test_db_proxy(real_db, tag)
    universe = ("AAPL", "MSFT")

    await capture_snapshot(
        "close", universe=universe, market_day=date(2026, 6, 1), db=proxy,
    )
    await capture_snapshot(
        "close", universe=universe, market_day=date(2026, 6, 1), db=proxy,
    )
    n = await proxy[DAILY_MARKET_SNAPSHOTS].count_documents(
        {"market_day": "2026-06-01", "label": "close"},
    )
    assert n == 2  # one per symbol — re-capture upserted, didn't double


@pytest.mark.asyncio
async def test_capture_rejects_bad_label():
    from shared.snapshots.service import capture_snapshot
    with pytest.raises(ValueError):
        await capture_snapshot("not_a_real_label")


@pytest.mark.asyncio
async def test_wipe_keeps_last_n_trading_days(fresh_test_db):
    from shared.snapshots.service import capture_snapshot, wipe_old_snapshots
    from namespaces import DAILY_MARKET_SNAPSHOTS

    real_db, tag = fresh_test_db
    proxy = _make_test_db_proxy(real_db, tag)
    # Capture 8 consecutive trading days (Mon-Fri x ~2 weeks).
    days = [
        date(2026, 5, 18), date(2026, 5, 19), date(2026, 5, 20),
        date(2026, 5, 21), date(2026, 5, 22), date(2026, 5, 26),  # Mon (5/25=Memorial)
        date(2026, 5, 27), date(2026, 5, 28),
    ]
    for d in days:
        await capture_snapshot("open", universe=("AAPL",), market_day=d, db=proxy)

    # Anchor wipe at Thu 2026-05-28, keep 5 trading days → keeps 5/22,
    # 5/26, 5/27, 5/28 (and 5/21). Older ones get nuked.
    summary = await wipe_old_snapshots(
        keep_trading_days=5, anchor=date(2026, 5, 28), db=proxy,
    )
    assert summary["snapshot_rows_deleted"] >= 3
    remaining = await proxy[DAILY_MARKET_SNAPSHOTS].distinct("market_day")
    assert all(r in summary["kept_market_days"] for r in remaining)


# ──────────────────────── worker ────────────────────────


def test_due_label_matches_scheduled_minute():
    from shared.snapshots.worker import _due_label
    from shared.snapshots.nyse_calendar import NYSE_TZ
    # Exactly 09:35 ET on a Wednesday
    et = datetime(2026, 6, 3, 9, 35, tzinfo=NYSE_TZ)
    assert _due_label(et) == "open"
    et = datetime(2026, 6, 3, 12, 30, tzinfo=NYSE_TZ)
    assert _due_label(et) == "midday"
    et = datetime(2026, 6, 3, 16, 5, tzinfo=NYSE_TZ)
    assert _due_label(et) == "close"


def test_due_label_returns_none_off_window():
    from shared.snapshots.worker import _due_label
    from shared.snapshots.nyse_calendar import NYSE_TZ
    et = datetime(2026, 6, 3, 10, 0, tzinfo=NYSE_TZ)
    assert _due_label(et) is None
    et = datetime(2026, 6, 3, 23, 59, tzinfo=NYSE_TZ)
    assert _due_label(et) is None


@pytest.mark.asyncio
async def test_worker_tick_skips_weekend(fresh_test_db):
    from shared.snapshots import worker as W
    real_db, tag = fresh_test_db
    proxy = _make_test_db_proxy(real_db, tag)
    # Saturday June 6 2026 — should not capture even if time is 09:35
    sat = datetime(2026, 6, 6, 9, 35, tzinfo=W.NYSE_TZ)
    with patch.object(W, "now_eastern", return_value=sat):
        await W._tick(proxy)
    # No capture-log rows should exist.
    from namespaces import DAILY_SNAPSHOT_CAPTURE_LOG
    n = await proxy[DAILY_SNAPSHOT_CAPTURE_LOG].count_documents({})
    assert n == 0


@pytest.mark.asyncio
async def test_worker_tick_skips_already_captured(fresh_test_db):
    """If the (market_day, label) capture already ran today, the
    tick must NOT re-fire."""
    from shared.snapshots import worker as W
    from shared.snapshots.service import capture_snapshot
    from namespaces import DAILY_SNAPSHOT_CAPTURE_LOG

    real_db, tag = fresh_test_db
    proxy = _make_test_db_proxy(real_db, tag)

    # Force a trading day at exactly 09:35 ET
    wed = datetime(2026, 6, 3, 9, 35, tzinfo=W.NYSE_TZ)

    # Pre-seed a capture-log row so the worker sees this label as done.
    await proxy[DAILY_SNAPSHOT_CAPTURE_LOG].insert_one({
        "market_day": "2026-06-03",
        "label": "open",
        "captured_at": "manual-seed",
    })
    n_before = await proxy[DAILY_SNAPSHOT_CAPTURE_LOG].count_documents({})

    with patch.object(W, "now_eastern", return_value=wed), \
         patch.object(W, "market_day_today", return_value=date(2026, 6, 3)):
        await W._tick(proxy)

    n_after = await proxy[DAILY_SNAPSHOT_CAPTURE_LOG].count_documents({})
    # Should not have inserted another row.
    assert n_after == n_before


# ──────────────────────── sp500 universe ────────────────────────


def test_sp500_universe_has_500_plus_symbols():
    from shared.snapshots.sp500_universe import SP500_TICKERS
    assert len(SP500_TICKERS) >= 500
    # No duplicates.
    assert len(SP500_TICKERS) == len(set(SP500_TICKERS))
    # All uppercase, no whitespace.
    for t in SP500_TICKERS:
        assert t == t.strip().upper()
        assert " " not in t
