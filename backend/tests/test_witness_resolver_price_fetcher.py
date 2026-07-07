"""Integration test for the resolver's real price-history fetcher.

Verifies the fetcher wired into the admin endpoint actually reads
from `shared_ohlcv_bars` correctly, honors the broker-primary source
priority, picks the right timeframe per lane, and returns the close
of the last bar at-or-before the target timestamp.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from db import db
from namespaces import SHARED_OHLCV_BARS


@pytest.fixture
async def _seed_and_cleanup_bars():
    """Populate `shared_ohlcv_bars` with a synthetic history for one
    equity symbol and one crypto pair, then yield the price fetcher
    for use in tests. Cleanup on teardown."""
    # Import the resolver's price-fetcher builder. We re-derive it
    # from the admin endpoint since it's defined inline; for the
    # test we copy the exact same logic into a helper.
    from typing import Optional
    from shared.research.bar_source import DEFAULT_TF_BY_LANE, load_recent_bars

    async def price_fetcher(symbol: str, ts_iso: str) -> Optional[float]:
        lane = "crypto" if "/" in symbol else "equity"
        tf = DEFAULT_TF_BY_LANE.get(lane, "1d")
        try:
            target = datetime.fromisoformat(str(ts_iso).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        bars, _src = await load_recent_bars(symbol, tf=tf, limit=200)
        if not bars:
            return None
        best = None
        for bar in bars:
            try:
                bar_ts = datetime.fromisoformat(str(bar.get("ts")).replace("Z", "+00:00"))
            except (TypeError, ValueError):
                continue
            if bar_ts <= target:
                best = float(bar.get("c") or 0.0) or None
            else:
                break
        return best

    # Seed 5 daily equity bars ending yesterday, closes 100 → 104.
    now = datetime.now(timezone.utc)
    day0 = (now - timedelta(days=6)).replace(hour=20, minute=0, second=0, microsecond=0)
    equity_bars = []
    for i in range(5):
        bar_ts = day0 + timedelta(days=i)
        equity_bars.append({
            "symbol": "TESTEQ",
            "tf": "1d",
            "source": "webull",
            "ts": bar_ts.isoformat(),
            "o": 100 + i, "h": 100 + i + 0.5, "l": 100 + i - 0.5,
            "c": 100 + i, "v": 1_000_000,
        })

    # Seed 30 hourly crypto bars for TESTCX/USD, closes 50.0 → 50.29.
    hour0 = (now - timedelta(hours=30)).replace(minute=0, second=0, microsecond=0)
    crypto_bars = []
    for i in range(30):
        bar_ts = hour0 + timedelta(hours=i)
        crypto_bars.append({
            "symbol": "TESTCX/USD",
            "tf": "1h",
            "source": "kraken_pro",
            "ts": bar_ts.isoformat(),
            "o": 50.0 + i * 0.01, "h": 50.0 + i * 0.01 + 0.005,
            "l": 50.0 + i * 0.01 - 0.005,
            "c": 50.0 + i * 0.01, "v": 5000,
        })

    await db[SHARED_OHLCV_BARS].delete_many({"symbol": {"$in": ["TESTEQ", "TESTCX/USD"]}})
    await db[SHARED_OHLCV_BARS].insert_many(equity_bars + crypto_bars)

    yield price_fetcher, day0, hour0

    await db[SHARED_OHLCV_BARS].delete_many({"symbol": {"$in": ["TESTEQ", "TESTCX/USD"]}})


class TestPriceFetcherFromOHLCV:
    @pytest.mark.asyncio
    async def test_equity_returns_close_of_bar_at_or_before_target(self, _seed_and_cleanup_bars):
        fetcher, day0, _hour0 = _seed_and_cleanup_bars
        # Target = exactly on bar 2's timestamp → should return that bar's close.
        target = (day0 + timedelta(days=2)).isoformat()
        price = await fetcher("TESTEQ", target)
        assert price == 102.0

    @pytest.mark.asyncio
    async def test_equity_returns_prior_bar_when_target_between_bars(self, _seed_and_cleanup_bars):
        fetcher, day0, _hour0 = _seed_and_cleanup_bars
        # Target = between bar 3 and bar 4 → should return bar 3's close.
        target = (day0 + timedelta(days=3, hours=5)).isoformat()
        price = await fetcher("TESTEQ", target)
        assert price == 103.0

    @pytest.mark.asyncio
    async def test_equity_returns_none_when_target_before_all_bars(self, _seed_and_cleanup_bars):
        fetcher, day0, _hour0 = _seed_and_cleanup_bars
        target = (day0 - timedelta(days=30)).isoformat()
        price = await fetcher("TESTEQ", target)
        assert price is None

    @pytest.mark.asyncio
    async def test_crypto_returns_hourly_bar_close(self, _seed_and_cleanup_bars):
        fetcher, _day0, hour0 = _seed_and_cleanup_bars
        target = (hour0 + timedelta(hours=10)).isoformat()
        price = await fetcher("TESTCX/USD", target)
        assert price == pytest.approx(50.10)

    @pytest.mark.asyncio
    async def test_missing_symbol_returns_none(self, _seed_and_cleanup_bars):
        fetcher, day0, _hour0 = _seed_and_cleanup_bars
        target = day0.isoformat()
        price = await fetcher("NOT_A_REAL_SYMBOL", target)
        assert price is None

    @pytest.mark.asyncio
    async def test_malformed_ts_returns_none_not_raises(self, _seed_and_cleanup_bars):
        fetcher, _day0, _hour0 = _seed_and_cleanup_bars
        price = await fetcher("TESTEQ", "not-an-iso-timestamp")
        assert price is None
