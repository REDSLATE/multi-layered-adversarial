"""Stage 2 finisher — equity `_fetch_mark_price` wiring tests.

Covers the two-tier equity mark-price resolver:
    1. Primary: Webull v2 `equity_snapshot` last-trade.
    2. Fallback: `shared_ohlcv_bars` latest-close (Polygon-fed).

The public contract (`_fetch_mark_price(lane, symbol)`) is preserved
so the existing resolver-level tests in `test_learning_live_loop.py`
that monkeypatch the same symbol continue to work.

Doctrine: NO paper / NO dry_run. These tests use monkeypatched
in-process fakes; no network, no market-hours dependency.
"""
from __future__ import annotations

import sys

import pytest

sys.path.insert(0, "/app/backend")

from db import db  # noqa: E402
from namespaces import SHARED_OHLCV_BARS  # noqa: E402
from shared.learning import outcome_resolver  # noqa: E402


_TEST_SYM = "MC-EQMARK-TEST"


@pytest.fixture(autouse=True)
async def _purge_bars():
    """Purge the synthetic bar row before + after each test."""
    await db[SHARED_OHLCV_BARS].delete_many({"symbol": _TEST_SYM})
    yield
    await db[SHARED_OHLCV_BARS].delete_many({"symbol": _TEST_SYM})


# ─── unknown lane / empty symbol ──────────────────────────────────


@pytest.mark.asyncio
async def test_fetch_mark_price_returns_none_for_empty_symbol():
    assert await outcome_resolver._fetch_mark_price("equity", "") is None


@pytest.mark.asyncio
async def test_fetch_mark_price_returns_none_for_unknown_lane():
    assert await outcome_resolver._fetch_mark_price("options", "AAPL") is None


# ─── Webull primary path ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_equity_mark_uses_webull_price_field(monkeypatch):
    """Snapshot returns `price` → we surface it directly."""

    class _FakeClient:
        def equity_snapshot(self, sym):
            assert sym == "AAPL"
            return {"price": 187.42, "ask": 187.45, "bid": 187.40}

    monkeypatch.setattr(
        "shared.market_data.webull_quotes.get_quotes_client",
        lambda: _FakeClient(),
    )

    mark = await outcome_resolver._fetch_mark_price("equity", "AAPL")
    assert mark == pytest.approx(187.42)


@pytest.mark.asyncio
async def test_equity_mark_prefers_price_over_ask(monkeypatch):
    """When both `price` and `ask` are present, last-trade wins."""

    class _FakeClient:
        def equity_snapshot(self, sym):
            return {"price": 100.0, "ask": 100.25}

    monkeypatch.setattr(
        "shared.market_data.webull_quotes.get_quotes_client",
        lambda: _FakeClient(),
    )

    mark = await outcome_resolver._fetch_mark_price("equity", "NVDA")
    assert mark == pytest.approx(100.0)


@pytest.mark.asyncio
async def test_equity_mark_falls_back_to_ask_when_price_missing(monkeypatch):
    """Some snapshots only carry a top-of-book quote; we accept `ask`."""

    class _FakeClient:
        def equity_snapshot(self, sym):
            return {"ask": 50.10}

    monkeypatch.setattr(
        "shared.market_data.webull_quotes.get_quotes_client",
        lambda: _FakeClient(),
    )

    mark = await outcome_resolver._fetch_mark_price("equity", "SPY")
    assert mark == pytest.approx(50.10)


@pytest.mark.asyncio
async def test_equity_mark_lowercases_symbol_normalised(monkeypatch):
    """Callers may pass lowercase; resolver upper-cases before lookup."""
    received: list[str] = []

    class _FakeClient:
        def equity_snapshot(self, sym):
            received.append(sym)
            return {"price": 42.0}

    monkeypatch.setattr(
        "shared.market_data.webull_quotes.get_quotes_client",
        lambda: _FakeClient(),
    )

    mark = await outcome_resolver._fetch_mark_price("equity", "aapl")
    assert mark == pytest.approx(42.0)
    assert received == ["AAPL"]


# ─── Fallback path ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_equity_mark_falls_back_to_bars_when_webull_none(monkeypatch):
    """Webull returns None → resolver reads the latest bar close."""

    class _EmptyClient:
        def equity_snapshot(self, sym):
            return None

    monkeypatch.setattr(
        "shared.market_data.webull_quotes.get_quotes_client",
        lambda: _EmptyClient(),
    )

    await db[SHARED_OHLCV_BARS].insert_one({
        "symbol": _TEST_SYM, "source": "polygon", "tf": "1d",
        "ts": "2026-02-18T21:00:00+00:00",
        "o": 10.0, "h": 11.0, "l": 9.5, "c": 10.75, "v": 1_000_000,
    })

    mark = await outcome_resolver._fetch_mark_price("equity", _TEST_SYM)
    assert mark == pytest.approx(10.75)


@pytest.mark.asyncio
async def test_equity_mark_bars_picks_most_recent_ts(monkeypatch):
    """Multiple bar rows → fallback returns the newest `ts`."""

    class _EmptyClient:
        def equity_snapshot(self, sym):
            return None

    monkeypatch.setattr(
        "shared.market_data.webull_quotes.get_quotes_client",
        lambda: _EmptyClient(),
    )

    await db[SHARED_OHLCV_BARS].insert_many([
        {"symbol": _TEST_SYM, "source": "polygon", "tf": "1d",
         "ts": "2026-02-17T21:00:00+00:00", "c": 10.0},
        {"symbol": _TEST_SYM, "source": "polygon", "tf": "1d",
         "ts": "2026-02-18T21:00:00+00:00", "c": 11.50},
        {"symbol": _TEST_SYM, "source": "finnhub", "tf": "5m",
         "ts": "2026-02-16T14:35:00+00:00", "c": 9.99},
    ])

    mark = await outcome_resolver._fetch_mark_price("equity", _TEST_SYM)
    assert mark == pytest.approx(11.50)


@pytest.mark.asyncio
async def test_equity_mark_returns_none_when_both_tiers_miss(monkeypatch):
    """Webull None + no bar rows → total miss → None (not a crash)."""

    class _EmptyClient:
        def equity_snapshot(self, sym):
            return None

    monkeypatch.setattr(
        "shared.market_data.webull_quotes.get_quotes_client",
        lambda: _EmptyClient(),
    )

    # No bars inserted → find_one returns None.
    mark = await outcome_resolver._fetch_mark_price("equity", _TEST_SYM)
    assert mark is None


@pytest.mark.asyncio
async def test_equity_mark_webull_exception_falls_through_to_bars(monkeypatch):
    """A Webull SDK exception must not kill the resolver — we fall
    through to the bars fallback."""

    class _BoomClient:
        def equity_snapshot(self, sym):
            raise RuntimeError("session expired")

    monkeypatch.setattr(
        "shared.market_data.webull_quotes.get_quotes_client",
        lambda: _BoomClient(),
    )

    await db[SHARED_OHLCV_BARS].insert_one({
        "symbol": _TEST_SYM, "source": "polygon", "tf": "1d",
        "ts": "2026-02-18T21:00:00+00:00", "c": 20.25,
    })

    mark = await outcome_resolver._fetch_mark_price("equity", _TEST_SYM)
    assert mark == pytest.approx(20.25)


@pytest.mark.asyncio
async def test_equity_mark_ignores_zero_and_negative_bars(monkeypatch):
    """A `c=0` (bad bar) must not surface as a valid mark."""

    class _EmptyClient:
        def equity_snapshot(self, sym):
            return None

    monkeypatch.setattr(
        "shared.market_data.webull_quotes.get_quotes_client",
        lambda: _EmptyClient(),
    )

    await db[SHARED_OHLCV_BARS].insert_one({
        "symbol": _TEST_SYM, "source": "polygon", "tf": "1d",
        "ts": "2026-02-18T21:00:00+00:00", "c": 0.0,
    })

    mark = await outcome_resolver._fetch_mark_price("equity", _TEST_SYM)
    assert mark is None
