"""Finnhub backfill tests.

Covered:
  - `backfill_one` happy path (stubbed `fetch_candles`).
  - `backfill_one` fetch_failed path (returns ok=False, doesn't raise).
  - `backfill_one` no_data path.
  - Idempotency: re-running same backfill upserts (count stays equal).
  - Bad-resolution rejection.
"""
from __future__ import annotations

import uuid

import pytest


@pytest.fixture
async def fresh_bars_collection(monkeypatch):
    from db import db as real_db
    from routes import finnhub_backfill as B
    tag = uuid.uuid4().hex[:8]
    coll_name = f"shared_ohlcv_bars_{tag}"
    monkeypatch.setattr(B, "SHARED_OHLCV_BARS", coll_name)
    try:
        yield real_db, coll_name
    finally:
        await real_db[coll_name].drop()


def _stub_candle_payload():
    """Mirrors Finnhub's /stock/candle response shape."""
    return {
        "s": "ok",
        "t": [1701302400, 1701388800, 1701475200],  # 3 days
        "o": [100.0, 101.0, 102.0],
        "h": [101.5, 102.5, 103.5],
        "l": [99.5, 100.5, 101.5],
        "c": [101.0, 102.0, 103.0],
        "v": [1_000_000, 1_200_000, 900_000],
    }


@pytest.mark.asyncio
async def test_backfill_one_happy_path(monkeypatch, fresh_bars_collection):
    real_db, coll = fresh_bars_collection
    from routes import finnhub_backfill as B

    async def _fake_fetch(symbol, resolution, frm, to, api_key):
        return _stub_candle_payload()

    monkeypatch.setattr(B, "fetch_candles", _fake_fetch)
    result = await B.backfill_one("NVDA", "D", api_key="x", lookback_years=10)
    assert result["ok"] is True
    assert result["bars_returned"] == 3
    assert result["bars_persisted"] == 3
    assert result["tf"] == "1d"
    n = await real_db[coll].count_documents({"source": "finnhub_equity"})
    assert n == 3


@pytest.mark.asyncio
async def test_backfill_one_no_data(monkeypatch, fresh_bars_collection):
    from routes import finnhub_backfill as B

    async def _fake_fetch(symbol, resolution, frm, to, api_key):
        return {"s": "no_data"}

    monkeypatch.setattr(B, "fetch_candles", _fake_fetch)
    result = await B.backfill_one("XYZ", "D", api_key="x")
    assert result["ok"] is True
    assert result["bars_persisted"] == 0
    assert result["reason"] == "no_data"


@pytest.mark.asyncio
async def test_backfill_one_fetch_failed(monkeypatch, fresh_bars_collection):
    from routes import finnhub_backfill as B

    async def _fake_fetch(symbol, resolution, frm, to, api_key):
        return None

    monkeypatch.setattr(B, "fetch_candles", _fake_fetch)
    result = await B.backfill_one("XYZ", "D", api_key="x")
    assert result["ok"] is False
    assert result["bars_persisted"] == 0
    assert "fetch_failed" in result["reason"]


@pytest.mark.asyncio
async def test_backfill_one_rejects_unknown_resolution(
    monkeypatch, fresh_bars_collection,
):
    from routes import finnhub_backfill as B
    result = await B.backfill_one("NVDA", "BOGUS", api_key="x")
    assert result["ok"] is False
    assert "unknown_resolution" in result["reason"]


@pytest.mark.asyncio
async def test_backfill_is_idempotent(monkeypatch, fresh_bars_collection):
    """Re-running the same backfill upserts; row count must not grow."""
    real_db, coll = fresh_bars_collection
    from routes import finnhub_backfill as B

    async def _fake_fetch(symbol, resolution, frm, to, api_key):
        return _stub_candle_payload()

    monkeypatch.setattr(B, "fetch_candles", _fake_fetch)
    await B.backfill_one("NVDA", "D", api_key="x")
    n1 = await real_db[coll].count_documents({"source": "finnhub_equity"})
    await B.backfill_one("NVDA", "D", api_key="x")
    n2 = await real_db[coll].count_documents({"source": "finnhub_equity"})
    assert n1 == n2 == 3
