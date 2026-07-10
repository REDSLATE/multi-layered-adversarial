"""Stage 2 finisher — equity `_fetch_mark_quote` wiring tests.

Covers the four-tier equity mark-price resolver:
    1. Webull v2 `equity_snapshot` last-trade (FRESH)
    2. `shared_ohlcv_bars` latest close (fresh iff bar age < window)
    3. Polygon `/v2/aggs/ticker/{t}/prev` (ALWAYS stale — diagnostic only)
    4. None

The resolver ONLY treats non-stale quotes as authoritative. Stale
quotes surface as diagnostic breadcrumbs via `_fetch_mark_quote` but
`_fetch_mark_price` (the legacy float shim) returns None for stale.

Doctrine: NO paper / NO dry_run. Tests use monkeypatched in-process
fakes; no network, no market-hours dependency, no POLYGON_API_KEY
consumption.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, "/app/backend")

from db import db  # noqa: E402
from namespaces import SHARED_OHLCV_BARS  # noqa: E402
from shared.learning import outcome_resolver  # noqa: E402
from shared.learning.outcome_resolver import MarkQuote  # noqa: E402


_TEST_SYM = "MC-EQMARK-TEST"


def _now():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.isoformat()


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
    assert await outcome_resolver._fetch_mark_quote("equity", "") is None


@pytest.mark.asyncio
async def test_fetch_mark_price_returns_none_for_unknown_lane():
    assert await outcome_resolver._fetch_mark_price("options", "AAPL") is None
    assert await outcome_resolver._fetch_mark_quote("options", "AAPL") is None


# ─── Webull primary path ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_equity_mark_uses_webull_price_field(monkeypatch):
    """Snapshot returns `price` → we surface it as a fresh quote."""

    class _FakeClient:
        def equity_snapshot(self, sym):
            assert sym == "AAPL"
            return {"price": 187.42, "ask": 187.45, "bid": 187.40}

    monkeypatch.setattr(
        "shared.market_data.webull_quotes.get_quotes_client",
        lambda: _FakeClient(),
    )

    quote = await outcome_resolver._fetch_mark_quote("equity", "AAPL")
    assert quote is not None
    assert quote.price == pytest.approx(187.42)
    assert quote.source == "webull_last_trade"
    assert quote.is_stale is False
    # Legacy shim returns the fresh float directly.
    assert await outcome_resolver._fetch_mark_price("equity", "AAPL") == \
        pytest.approx(187.42)


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

    quote = await outcome_resolver._fetch_mark_quote("equity", "NVDA")
    assert quote.price == pytest.approx(100.0)
    assert quote.source == "webull_last_trade"


@pytest.mark.asyncio
async def test_equity_mark_falls_back_to_ask_when_price_missing(monkeypatch):
    """Snapshots that only carry a top-of-book quote → we accept `ask`."""

    class _FakeClient:
        def equity_snapshot(self, sym):
            return {"ask": 50.10}

    monkeypatch.setattr(
        "shared.market_data.webull_quotes.get_quotes_client",
        lambda: _FakeClient(),
    )

    quote = await outcome_resolver._fetch_mark_quote("equity", "SPY")
    assert quote.price == pytest.approx(50.10)
    assert quote.is_stale is False


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

    quote = await outcome_resolver._fetch_mark_quote("equity", "aapl")
    assert quote.price == pytest.approx(42.0)
    assert received == ["AAPL"]


# ─── Bars fallback tier — freshness gating ────────────────────────


@pytest.mark.asyncio
async def test_equity_mark_bars_fresh_returns_non_stale(monkeypatch):
    """Webull None + a bar dated NOW → fresh fallback wins."""

    class _EmptyClient:
        def equity_snapshot(self, sym):
            return None

    monkeypatch.setattr(
        "shared.market_data.webull_quotes.get_quotes_client",
        lambda: _EmptyClient(),
    )

    fresh_ts = _iso(_now() - timedelta(minutes=2))
    await db[SHARED_OHLCV_BARS].insert_one({
        "symbol": _TEST_SYM, "source": "polygon", "tf": "5m",
        "ts": fresh_ts, "c": 10.75,
    })

    quote = await outcome_resolver._fetch_mark_quote("equity", _TEST_SYM)
    assert quote is not None
    assert quote.price == pytest.approx(10.75)
    assert quote.source == "ohlcv_bars_intraday"
    assert quote.is_stale is False
    # Legacy shim returns the price.
    assert await outcome_resolver._fetch_mark_price("equity", _TEST_SYM) == \
        pytest.approx(10.75)


@pytest.mark.asyncio
async def test_equity_mark_bars_stale_is_flagged_and_shim_returns_none(
    monkeypatch,
):
    """Webull None + only a very old bar → quote returned STALE.
    Legacy shim (`_fetch_mark_price`) must return None for stale."""

    class _EmptyClient:
        def equity_snapshot(self, sym):
            return None

    # No Polygon key → polygon tier short-circuits to None.
    monkeypatch.setenv("POLYGON_API_KEY", "")
    monkeypatch.setattr(
        "shared.market_data.webull_quotes.get_quotes_client",
        lambda: _EmptyClient(),
    )

    stale_ts = _iso(_now() - timedelta(hours=6))
    await db[SHARED_OHLCV_BARS].insert_one({
        "symbol": _TEST_SYM, "source": "polygon", "tf": "1d",
        "ts": stale_ts, "c": 22.00,
    })

    quote = await outcome_resolver._fetch_mark_quote("equity", _TEST_SYM)
    assert quote is not None
    assert quote.is_stale is True
    assert quote.source == "ohlcv_bars_stale"
    # The float shim refuses stale — returns None so the resolver
    # bumps `skipped_stale_mark` instead of writing a bad outcome.
    assert await outcome_resolver._fetch_mark_price("equity", _TEST_SYM) is None


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

    newest_ts = _iso(_now() - timedelta(minutes=3))
    await db[SHARED_OHLCV_BARS].insert_many([
        {"symbol": _TEST_SYM, "source": "polygon", "tf": "1d",
         "ts": _iso(_now() - timedelta(days=1)), "c": 10.0},
        {"symbol": _TEST_SYM, "source": "polygon", "tf": "5m",
         "ts": newest_ts, "c": 11.50},
        {"symbol": _TEST_SYM, "source": "finnhub", "tf": "5m",
         "ts": _iso(_now() - timedelta(hours=2)), "c": 9.99},
    ])

    quote = await outcome_resolver._fetch_mark_quote("equity", _TEST_SYM)
    assert quote.price == pytest.approx(11.50)
    assert quote.is_stale is False


@pytest.mark.asyncio
async def test_equity_mark_returns_none_when_all_tiers_miss(monkeypatch):
    """Webull None + no bars + no Polygon key → None."""

    class _EmptyClient:
        def equity_snapshot(self, sym):
            return None

    monkeypatch.setenv("POLYGON_API_KEY", "")
    monkeypatch.setattr(
        "shared.market_data.webull_quotes.get_quotes_client",
        lambda: _EmptyClient(),
    )

    quote = await outcome_resolver._fetch_mark_quote("equity", _TEST_SYM)
    assert quote is None
    assert await outcome_resolver._fetch_mark_price("equity", _TEST_SYM) is None


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

    fresh_ts = _iso(_now() - timedelta(minutes=1))
    await db[SHARED_OHLCV_BARS].insert_one({
        "symbol": _TEST_SYM, "source": "polygon", "tf": "5m",
        "ts": fresh_ts, "c": 20.25,
    })

    quote = await outcome_resolver._fetch_mark_quote("equity", _TEST_SYM)
    assert quote is not None
    assert quote.price == pytest.approx(20.25)
    assert quote.is_stale is False


@pytest.mark.asyncio
async def test_equity_mark_ignores_zero_and_negative_bars(monkeypatch):
    """A `c=0` (bad bar) must not surface as a valid mark."""

    class _EmptyClient:
        def equity_snapshot(self, sym):
            return None

    monkeypatch.setenv("POLYGON_API_KEY", "")
    monkeypatch.setattr(
        "shared.market_data.webull_quotes.get_quotes_client",
        lambda: _EmptyClient(),
    )

    await db[SHARED_OHLCV_BARS].insert_one({
        "symbol": _TEST_SYM, "source": "polygon", "tf": "5m",
        "ts": _iso(_now()), "c": 0.0,
    })

    quote = await outcome_resolver._fetch_mark_quote("equity", _TEST_SYM)
    assert quote is None


# ─── Polygon prev-close tier (ALWAYS stale) ───────────────────────


@pytest.mark.asyncio
async def test_polygon_prev_close_is_always_stale(monkeypatch):
    """Even a valid Polygon response is marked stale — the Starter
    plan can't give us intraday last-trade, so the value MUST NOT
    resolve a 5m/15m/1h horizon."""
    import httpx

    class _FakeClient:
        def equity_snapshot(self, sym):
            return None

    monkeypatch.setattr(
        "shared.market_data.webull_quotes.get_quotes_client",
        lambda: _FakeClient(),
    )
    monkeypatch.setenv("POLYGON_API_KEY", "test-poly-key")

    class _MockResp:
        status_code = 200

        def json(self):
            return {
                "status": "OK",
                "results": [{"c": 195.55, "t": 1739923200000}],
            }

    class _MockClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, path, params=None):
            assert "/prev" in path
            return _MockResp()

    monkeypatch.setattr(httpx, "AsyncClient", _MockClient)

    # No Webull, no bars → resolver walks all the way to Polygon.
    quote = await outcome_resolver._fetch_mark_quote("equity", "MSFT")
    assert quote is not None
    assert quote.price == pytest.approx(195.55)
    assert quote.source == "polygon_prev_close"
    assert quote.is_stale is True
    # Legacy shim refuses stale.
    assert await outcome_resolver._fetch_mark_price("equity", "MSFT") is None


@pytest.mark.asyncio
async def test_polygon_skipped_when_no_api_key(monkeypatch):
    """No POLYGON_API_KEY → tier 3 short-circuits to None; overall
    result is None (no fresh quote and no stale diagnostic)."""

    class _EmptyClient:
        def equity_snapshot(self, sym):
            return None

    monkeypatch.setattr(
        "shared.market_data.webull_quotes.get_quotes_client",
        lambda: _EmptyClient(),
    )
    monkeypatch.setenv("POLYGON_API_KEY", "")

    quote = await outcome_resolver._fetch_mark_quote("equity", _TEST_SYM)
    assert quote is None


# ─── Crypto path — Kraken (always fresh) ──────────────────────────


@pytest.mark.asyncio
async def test_crypto_mark_returns_fresh_kraken_quote(monkeypatch):
    async def _fake_ticker(pair):
        assert pair == "XBTUSD"  # BTC → XBT quirk
        return 61234.5

    monkeypatch.setattr(
        "shared.crypto.broker_adapter._ticker_price",
        _fake_ticker,
    )

    quote = await outcome_resolver._fetch_mark_quote("crypto", "BTC/USD")
    assert quote is not None
    assert quote.price == pytest.approx(61234.5)
    assert quote.source == "kraken_ticker"
    assert quote.is_stale is False
