"""Tests for /app/trader/spread_stream.py — Webull MQTT L1 stream.

Doctrine: no real network. We build a fake `quotes` payload matching
the `DataStreamingClient` message shape and drive `_on_quote_message`
directly, then assert that the cache + SQLite tape both received the
tick.
"""
from __future__ import annotations

import asyncio
import sys

import pytest

sys.path.insert(0, "/app")

from trader import spread, spread_stream, store  # noqa: E402


class _FakeAskBid:
    """Duck-types the SDK's ask/bid entries — has `.get_price()`."""

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


class _FakeQuotes:
    """Duck-types the object dispatched by DataStreamingClient into
    `on_quotes_message(client, topic, quotes)`. Has get_asks(),
    get_bids(), get_basic()."""

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
    spread_stream._status.update({
        "state": "stopped", "message_count": 0,
        "last_message_at": None, "last_error": None,
        "subscribed_symbols": [],
    })
    yield
    store.close()
    loop.close()
    asyncio.set_event_loop(None)


def test_on_quote_message_ignores_unrelated_topics(fresh_store):
    """SNAPSHOT/TICK payloads without get_asks/get_bids must no-op."""

    class NotAQuote:
        pass

    spread_stream._on_quote_message(None, "snapshot", NotAQuote())
    assert spread.latest("TSLA") == {}
    assert spread_stream.get_status()["message_count"] == 0


def test_on_quote_message_updates_cache_and_store(fresh_store):
    """Duck-typed QuoteResult -> _latest cache + spread_tick row."""
    fake = _FakeQuotes(
        symbol="TSLA",
        bids=[("426.50", "200"), ("426.45", "500")],
        asks=[("426.55", "100"), ("426.60", "300")],
    )
    spread_stream._on_quote_message(None, "quote", fake)

    cached = spread.latest("TSLA")
    assert cached
    assert cached["bid"] == 426.50
    assert cached["ask"] == 426.55
    assert cached["source"] == "webull_mqtt"
    assert cached["lane"] == "equity"
    assert cached["spread_bps"] == pytest.approx(1.17, abs=0.05)
    hist = store.recent_spread_ticks(pair="TSLA", limit=5)
    assert len(hist) == 1
    assert hist[0]["source"] == "webull_mqtt"
    st = spread_stream.get_status()
    assert st["message_count"] == 1
    assert st["last_message_at"]


def test_on_quote_message_accepts_dict_shape(fresh_store):
    """Some SDK versions dispatch raw dicts — parser handles both."""
    payload = {
        "symbol": "AAPL",
        "bidList": [{"price": "185.40"}],
        "askList": [{"price": "185.44"}],
    }
    spread_stream._on_quote_message(None, "quote", payload)
    cached = spread.latest("AAPL")
    assert cached
    assert cached["bid"] == 185.40
    assert cached["ask"] == 185.44


def test_on_quote_message_rejects_crossed_book(fresh_store):
    fake = _FakeQuotes(
        symbol="TSLA",
        bids=[("500.00", "1")],
        asks=[("400.00", "1")],
    )
    spread_stream._on_quote_message(None, "quote", fake)
    assert spread.latest("TSLA") == {}


def test_on_quote_message_ignores_empty_book(fresh_store):
    fake = _FakeQuotes(symbol="TSLA", bids=[], asks=[])
    spread_stream._on_quote_message(None, "quote", fake)
    assert spread.latest("TSLA") == {}
    assert spread_stream.get_status()["message_count"] == 0


def test_start_is_noop_when_disabled(fresh_store, monkeypatch):
    monkeypatch.setenv("TRADER_EQUITY_STREAM_ENABLED", "false")
    spread_stream.start()
    assert (
        spread_stream._thread is None
        or not spread_stream._thread.is_alive()
    )


def test_start_needs_credentials(fresh_store, monkeypatch):
    monkeypatch.setenv("TRADER_EQUITY_STREAM_ENABLED", "true")
    monkeypatch.delenv("WEBULL_APP_KEY", raising=False)
    monkeypatch.delenv("WEBULL_APP_SECRET", raising=False)
    monkeypatch.setenv("TRADER_EQUITY_SPREAD_TICKERS", "TSLA")

    spread_stream.start()
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
