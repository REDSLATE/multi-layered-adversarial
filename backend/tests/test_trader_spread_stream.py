"""Tests for /app/trader/spread_stream.py — Webull MQTT L1 stream.

Doctrine: no real network. We mock the SDK's QuoteResult and drive
`_on_quote_message` directly, then assert that the cache + SQLite
tape both received the tick.
"""
from __future__ import annotations

import asyncio
import sys

import pytest

sys.path.insert(0, "/app")

from trader import spread, spread_stream, store  # noqa: E402


class _FakeAskBid:
    def __init__(self, price, size="0"):
        self._price = str(price)
        self._size = str(size)

    def get_price(self):
        return self._price

    def get_size(self):
        return self._size


class _FakeBasic:
    def __init__(self, symbol):
        self._symbol = symbol

    def get_symbol(self):
        return self._symbol

    def get_timestamp(self):
        return "1720000000000"


class _FakeQuoteResult:
    """Duck-types the real QuoteResult from
    `webullsdkmdata.quotes.subscribe.quote_result.QuoteResult`."""

    def __init__(self, symbol, bids, asks):
        self._basic = _FakeBasic(symbol)
        self._bids = [_FakeAskBid(p, s) for p, s in bids]
        self._asks = [_FakeAskBid(p, s) for p, s in asks]

    def get_basic(self):
        return self._basic

    def get_bids(self):
        return self._bids

    def get_asks(self):
        return self._asks


@pytest.fixture()
def fresh_store(tmp_path, monkeypatch):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    store.init(
        str(tmp_path / "executions.sqlite"),
        str(tmp_path / "jsonl"),
    )
    spread._latest.clear()
    # Reset stream status between tests
    spread_stream._status.update({
        "state": "stopped", "message_count": 0,
        "last_message_at": None, "last_error": None,
        "subscribed_symbols": [],
    })
    yield
    store.close()
    loop.close()
    asyncio.set_event_loop(None)


def test_on_quote_message_ignores_non_quote_result(fresh_store, monkeypatch):
    """The SDK dispatches multiple result types to the same callback;
    we only care about QuoteResult. Anything else is a no-op."""
    # Import so the isinstance check has the real class in scope
    from webullsdkmdata.quotes.subscribe.quote_result import QuoteResult

    class NotAQuote:
        pass

    spread_stream._on_quote_message(None, None, NotAQuote())
    assert spread.latest("TSLA") == {}
    assert spread_stream.get_status()["message_count"] == 0


def test_on_quote_message_updates_cache_and_store(fresh_store, monkeypatch):
    """Isinstance guard uses the real QuoteResult; we monkeypatch the
    imported class inside spread_stream to accept our fake."""
    from webullsdkmdata.quotes.subscribe import quote_result as _qr_mod
    monkeypatch.setattr(_qr_mod, "QuoteResult", _FakeQuoteResult)

    fake = _FakeQuoteResult(
        symbol="TSLA",
        bids=[("426.50", "200"), ("426.45", "500")],
        asks=[("426.55", "100"), ("426.60", "300")],
    )
    spread_stream._on_quote_message(None, None, fake)

    # In-memory cache updated
    cached = spread.latest("TSLA")
    assert cached
    assert cached["bid"] == 426.50
    assert cached["ask"] == 426.55
    assert cached["source"] == "webull_mqtt"
    assert cached["lane"] == "equity"
    # spread bps ≈ 0.05 / 426.525 * 10_000 ≈ 1.17 bps
    assert cached["spread_bps"] == pytest.approx(1.17, abs=0.05)
    # SQLite tape updated
    hist = store.recent_spread_ticks(pair="TSLA", limit=5)
    assert len(hist) == 1
    assert hist[0]["source"] == "webull_mqtt"
    # Status counter incremented
    st = spread_stream.get_status()
    assert st["message_count"] == 1
    assert st["last_message_at"]


def test_on_quote_message_rejects_crossed_book(fresh_store, monkeypatch):
    """Bid > ask (crossed market) must not corrupt the cache."""
    from webullsdkmdata.quotes.subscribe import quote_result as _qr_mod
    monkeypatch.setattr(_qr_mod, "QuoteResult", _FakeQuoteResult)

    fake = _FakeQuoteResult(
        symbol="TSLA",
        bids=[("500.00", "1")],
        asks=[("400.00", "1")],
    )
    spread_stream._on_quote_message(None, None, fake)
    assert spread.latest("TSLA") == {}


def test_on_quote_message_ignores_empty_book(fresh_store, monkeypatch):
    from webullsdkmdata.quotes.subscribe import quote_result as _qr_mod
    monkeypatch.setattr(_qr_mod, "QuoteResult", _FakeQuoteResult)

    fake = _FakeQuoteResult(symbol="TSLA", bids=[], asks=[])
    spread_stream._on_quote_message(None, None, fake)
    assert spread.latest("TSLA") == {}
    assert spread_stream.get_status()["message_count"] == 0


def test_start_is_noop_when_disabled(fresh_store, monkeypatch):
    monkeypatch.setenv("TRADER_EQUITY_STREAM_ENABLED", "false")
    spread_stream.start()
    # No thread started
    assert spread_stream._thread is None or not spread_stream._thread.is_alive()


def test_start_needs_credentials(fresh_store, monkeypatch):
    """When creds are missing, start() must not raise; status flips to error."""
    monkeypatch.setenv("TRADER_EQUITY_STREAM_ENABLED", "true")
    monkeypatch.delenv("WEBULL_APP_KEY", raising=False)
    monkeypatch.delenv("WEBULL_APP_SECRET", raising=False)
    monkeypatch.setenv("TRADER_EQUITY_SPREAD_TICKERS", "TSLA")

    spread_stream.start()
    # Give the thread a moment to hit the creds check and bail
    import time
    for _ in range(20):
        if spread_stream.get_status()["state"] == "error":
            break
        time.sleep(0.05)
    st = spread_stream.get_status()
    assert st["state"] == "error"
    assert st["last_error"] == "creds_missing"
    spread_stream.stop(timeout=1.0)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
