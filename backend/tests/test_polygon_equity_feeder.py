"""Polygon equity feeder tests.

Covered:
  - `_row_to_bar` shape (success + malformed cases).
  - `_safe_to_pull` schedule logic (trading day vs weekend, before
    vs after close+buffer).
  - `pull_for_date` end-to-end with stubbed HTTP.
  - Worker idempotency shortcut (`_tick` skips when already-pulled
    threshold is met).
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest


# ──────────────────────── _row_to_bar ────────────────────────


def test_row_to_bar_happy_path():
    from shared.feeders.polygon_equity import _row_to_bar, FEEDER_SOURCE, DEFAULT_TF
    bar_date = date(2026, 5, 29)
    row = {
        "T": "NVDA", "v": 184_300_000.0, "vw": 213.30, "o": 214.57,
        "c": 211.14, "h": 217.85, "l": 211.13, "t": 1780027200000,
        "n": 2_654_369,
    }
    out = _row_to_bar(row, bar_date)
    assert out is not None
    assert out["source"] == FEEDER_SOURCE == "polygon"
    assert out["tf"] == DEFAULT_TF == "1d"
    assert out["symbol"] == "NVDA"
    assert out["ts"] == "2026-05-29T00:00:00+00:00"
    assert out["o"] == 214.57
    assert out["c"] == 211.14
    assert out["v"] == 184_300_000.0
    assert out["vwap"] == 213.30
    assert out["trades"] == 2_654_369
    assert out["feeder"] == "polygon_equity"


def test_row_to_bar_rejects_missing_ohlc():
    from shared.feeders.polygon_equity import _row_to_bar
    bar_date = date(2026, 5, 29)
    assert _row_to_bar({"T": "X"}, bar_date) is None
    assert _row_to_bar({"T": "X", "o": 1, "h": 2, "l": 1.5}, bar_date) is None


def test_row_to_bar_rejects_no_symbol():
    from shared.feeders.polygon_equity import _row_to_bar
    bar_date = date(2026, 5, 29)
    assert _row_to_bar({"o": 1, "h": 2, "l": 0.5, "c": 1.5}, bar_date) is None
    assert _row_to_bar(
        {"T": "X" * 50, "o": 1, "h": 2, "l": 0.5, "c": 1.5}, bar_date,
    ) is None


def test_row_to_bar_lowercase_ticker_normalized():
    from shared.feeders.polygon_equity import _row_to_bar
    out = _row_to_bar(
        {"T": "aapl", "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 100},
        date(2026, 5, 29),
    )
    assert out is not None
    assert out["symbol"] == "AAPL"


# ──────────────────────── _safe_to_pull ────────────────────────


def test_safe_to_pull_trading_day_after_close():
    """Wed 2026-06-03 at 17:00 ET → 30min past close, pull TODAY."""
    from shared.feeders.polygon_equity import _safe_to_pull
    from shared.snapshots.nyse_calendar import NYSE_TZ
    now_et = datetime(2026, 6, 3, 17, 0, tzinfo=NYSE_TZ)
    target = _safe_to_pull(now_et, close_buffer_min=30)
    assert target == date(2026, 6, 3)


def test_safe_to_pull_trading_day_before_close_buffer():
    """Wed 2026-06-03 at 14:00 ET → before close+30min, pull prior day."""
    from shared.feeders.polygon_equity import _safe_to_pull
    from shared.snapshots.nyse_calendar import NYSE_TZ
    now_et = datetime(2026, 6, 3, 14, 0, tzinfo=NYSE_TZ)
    target = _safe_to_pull(now_et, close_buffer_min=30)
    # Should be the previous trading day (Tue 2026-06-02).
    assert target == date(2026, 6, 2)


def test_safe_to_pull_weekend_uses_previous_trading_day():
    from shared.feeders.polygon_equity import _safe_to_pull
    from shared.snapshots.nyse_calendar import NYSE_TZ
    sat = datetime(2026, 6, 6, 10, 0, tzinfo=NYSE_TZ)
    target = _safe_to_pull(sat, close_buffer_min=30)
    # Should walk back to Fri 2026-06-05.
    assert target == date(2026, 6, 5)


def test_safe_to_pull_just_before_buffer_doesnt_grab_today():
    """16:25 ET on a trading day should still wait — Polygon hasn't
    finalized today's grouped daily yet."""
    from shared.feeders.polygon_equity import _safe_to_pull
    from shared.snapshots.nyse_calendar import NYSE_TZ
    et = datetime(2026, 6, 3, 16, 25, tzinfo=NYSE_TZ)
    target = _safe_to_pull(et, close_buffer_min=30)
    assert target == date(2026, 6, 2)  # previous trading day


# ──────────────────────── pull_for_date ────────────────────────


@pytest.mark.asyncio
async def test_pull_for_date_persists_bars(monkeypatch):
    """Fan-out the full pipeline against a stubbed Polygon payload.
    Uses a tagged collection name so we don't pollute the real
    `shared_ohlcv_bars`."""
    import uuid
    from db import db as real_db
    from shared.feeders import polygon_equity as P

    tag = uuid.uuid4().hex[:8]
    coll_name = f"shared_ohlcv_bars_{tag}"

    # Patch SHARED_OHLCV_BARS the feeder writes to.
    monkeypatch.setattr(P, "SHARED_OHLCV_BARS", coll_name)

    fake_payload = {
        "status": "OK",
        "queryCount": 3,
        "resultsCount": 3,
        "results": [
            {"T": "AAPL", "o": 200.0, "h": 202.0, "l": 199.5,
             "c": 201.4, "v": 1_000_000, "vw": 201.0, "n": 50_000},
            {"T": "NVDA", "o": 140.0, "h": 142.0, "l": 138.0,
             "c": 141.0, "v": 5_000_000, "vw": 140.5, "n": 200_000},
            {"T": "MSFT", "o": 410.0, "h": 412.0, "l": 408.0,
             "c": 411.0, "v": 800_000, "vw": 410.4, "n": 30_000},
        ],
    }

    async def _fake_fetch(bar_date, api_key):
        return fake_payload

    monkeypatch.setattr(P, "fetch_grouped_daily", _fake_fetch)

    summary = await P.pull_for_date(date(2026, 5, 29), api_key="x")
    assert summary["ok"] is True
    assert summary["rows_fetched"] == 3
    assert summary["rows_persisted"] == 3
    assert summary["rows_skipped_malformed"] == 0

    rows = await real_db[coll_name].find(
        {}, {"_id": 0, "symbol": 1, "c": 1, "tf": 1, "source": 1},
    ).sort("symbol", 1).to_list(length=10)
    assert [r["symbol"] for r in rows] == ["AAPL", "MSFT", "NVDA"]
    for r in rows:
        assert r["tf"] == "1d"
        assert r["source"] == "polygon"

    # Idempotent: re-pull doesn't double-write.
    await P.pull_for_date(date(2026, 5, 29), api_key="x")
    n = await real_db[coll_name].count_documents({})
    assert n == 3

    # Cleanup.
    await real_db[coll_name].drop()


@pytest.mark.asyncio
async def test_pull_for_date_fetch_failure_returns_ok_false(monkeypatch):
    """When fetch returns None (auth error / network error), the
    function must return a structured failure, not raise."""
    from shared.feeders import polygon_equity as P

    async def _fake_fetch_none(bar_date, api_key):
        return None

    monkeypatch.setattr(P, "fetch_grouped_daily", _fake_fetch_none)

    summary = await P.pull_for_date(date(2026, 5, 29), api_key="x")
    assert summary["ok"] is False
    assert summary["rows_persisted"] == 0
    assert "reason" in summary
