"""Tests for /app/trader/spread.py — spread poller (Kraken + Webull).

Uses local httpx mocks and a fresh SQLite store per test. Doctrine:
no Mongo, no network. Every assertion runs offline.
"""
from __future__ import annotations

import asyncio
import os
import sys

import httpx
import pytest

sys.path.insert(0, "/app")

from trader import store, spread, config  # noqa: E402


# ─── shared fixtures ──────────────────────────────────────────────

@pytest.fixture()
def fresh_store(tmp_path):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    store.init(
        str(tmp_path / "executions.sqlite"),
        str(tmp_path / "jsonl"),
    )
    # Clear the in-memory cache between tests
    spread._latest.clear()
    yield
    store.close()
    loop.close()
    asyncio.set_event_loop(None)


def _mock_transport(handler):
    return httpx.MockTransport(handler)


# ─── Kraken parsing ───────────────────────────────────────────────

def test_parse_kraken_computes_spread_bps(fresh_store):
    result = {
        "XXBTZUSD": {
            "a": ["45010.5", "1", "1.000"],
            "b": ["45000.0", "1", "1.000"],
            "c": ["45005.0", "0.01"],
        }
    }
    row = spread._parse_kraken("XBTUSD", result)
    assert row is not None
    assert row["pair"] == "XBTUSD"
    assert row["lane"] == "crypto"
    assert row["source"] == "kraken"
    assert row["bid"] == pytest.approx(45000.0)
    assert row["ask"] == pytest.approx(45010.5)
    assert row["last"] == pytest.approx(45005.0)
    # spread = (10.5 / 45005.25) * 10000 ≈ 2.33 bps
    assert row["spread_bps"] == pytest.approx(2.3331, abs=0.01)


def test_parse_kraken_rejects_negative_or_crossed_book(fresh_store):
    crossed = {"X": {"a": ["100"], "b": ["200"], "c": ["150"]}}
    assert spread._parse_kraken("X", crossed) is None
    zero = {"X": {"a": ["0"], "b": ["0"], "c": ["0"]}}
    assert spread._parse_kraken("X", zero) is None


@pytest.mark.asyncio
async def test_fetch_kraken_success(fresh_store):
    def handler(request: httpx.Request) -> httpx.Response:
        assert "Ticker" in str(request.url)
        return httpx.Response(200, json={
            "error": [],
            "result": {
                "XXBTZUSD": {
                    "a": ["50100", "1", "1"],
                    "b": ["50000", "1", "1"],
                    "c": ["50050", "0.01"],
                }
            },
        })
    async with httpx.AsyncClient(transport=_mock_transport(handler)) as client:
        row = await spread.fetch_kraken(client, "XBTUSD")
    assert row is not None
    assert row["bid"] == 50000.0
    assert row["ask"] == 50100.0


@pytest.mark.asyncio
async def test_fetch_kraken_api_error_returns_none(fresh_store):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "error": ["EQuery:Unknown asset pair"], "result": {},
        })
    async with httpx.AsyncClient(transport=_mock_transport(handler)) as client:
        row = await spread.fetch_kraken(client, "BOGUS")
    assert row is None


# ─── Webull OpenAPI parsing + fetch ───────────────────────────────

def test_parse_webull_extracts_bid_ask_from_snapshot(fresh_store):
    payload = {
        "result": True,
        "msg": "Success",
        "data": {
            "symbol": "TSLA",
            "latestPrice": 240.55,
            "quotes": {
                "bidPrice": 240.50, "bidSize": 200,
                "askPrice": 240.60, "askSize": 150,
            },
        },
    }
    row = spread._parse_webull_snapshot("TSLA", payload)
    assert row is not None
    assert row["pair"] == "TSLA"
    assert row["lane"] == "equity"
    assert row["source"] == "webull"
    assert row["bid"] == pytest.approx(240.50)
    assert row["ask"] == pytest.approx(240.60)
    assert row["last"] == pytest.approx(240.55)


def test_parse_webull_tolerates_snake_case(fresh_store):
    """Regional endpoints emit snake_case; parser must handle both."""
    payload = {
        "data": {
            "latest_price": 100.0,
            "quotes": {"bid_price": 99.90, "ask_price": 100.10},
        },
    }
    row = spread._parse_webull_snapshot("X", payload)
    assert row is not None
    assert row["bid"] == 99.90
    assert row["ask"] == 100.10


@pytest.mark.asyncio
async def test_fetch_webull_uses_openapi_snapshot(fresh_store, monkeypatch):
    monkeypatch.setenv("WEBULL_APP_KEY", "test-key")
    monkeypatch.setenv("WEBULL_APP_SECRET", "test-secret")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["key"] = request.headers.get("X-Request-App-Key")
        seen["secret"] = request.headers.get("X-Request-App-Secret")
        return httpx.Response(200, json={
            "result": True,
            "data": {
                "symbol": "TSLA", "latestPrice": 240.55,
                "quotes": {"bidPrice": 240.50, "askPrice": 240.60},
            },
        })

    async with httpx.AsyncClient(transport=_mock_transport(handler)) as client:
        row = await spread.fetch_webull(client, "TSLA")

    assert row is not None
    assert row["bid"] == pytest.approx(240.50)
    assert row["ask"] == pytest.approx(240.60)
    assert "openapi/market-data/stock/snapshot" in seen["url"]
    assert "symbol=TSLA" in seen["url"]
    assert seen["key"] == "test-key"
    assert seen["secret"] == "test-secret"


@pytest.mark.asyncio
async def test_fetch_webull_missing_creds_returns_none(fresh_store, monkeypatch):
    monkeypatch.delenv("WEBULL_APP_KEY", raising=False)
    monkeypatch.delenv("WEBULL_APP_SECRET", raising=False)

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("must not hit network when creds are missing")

    async with httpx.AsyncClient(transport=_mock_transport(handler)) as client:
        row = await spread.fetch_webull(client, "TSLA")
    assert row is None


@pytest.mark.asyncio
async def test_fetch_webull_returns_none_on_403(fresh_store, monkeypatch):
    """Auth or entitlement failures must not crash the poller."""
    monkeypatch.setenv("WEBULL_APP_KEY", "test-key")
    monkeypatch.setenv("WEBULL_APP_SECRET", "test-secret")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"code": "SUBSCRIPTION_REQUIRED"})

    async with httpx.AsyncClient(transport=_mock_transport(handler)) as client:
        row = await spread.fetch_webull(client, "TSLA")
    assert row is None


# ─── caching + persistence ───────────────────────────────────────

@pytest.mark.asyncio
async def test_poll_kraken_once_records_and_caches(fresh_store, monkeypatch):
    monkeypatch.setenv("TRADER_SPREAD_PAIRS", "XBTUSD")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "error": [],
            "result": {
                "XXBTZUSD": {
                    "a": ["50100", "1", "1"], "b": ["50000", "1", "1"],
                    "c": ["50050", "0.01"],
                }
            },
        })
    async with httpx.AsyncClient(transport=_mock_transport(handler)) as client:
        out = await spread.poll_kraken_once(client)

    assert len(out) == 1
    # In-memory cache updated
    cached = spread.latest("XBTUSD")
    assert cached and cached["bid"] == 50000.0
    # SQLite tape written
    hist = store.recent_spread_ticks(pair="XBTUSD", limit=10)
    assert len(hist) == 1
    assert hist[0]["source"] == "kraken"


@pytest.mark.asyncio
async def test_poll_webull_once_records_and_caches(fresh_store, monkeypatch):
    monkeypatch.setenv("TRADER_EQUITY_SPREAD_TICKERS", "TSLA")
    monkeypatch.setenv("WEBULL_APP_KEY", "test-key")
    monkeypatch.setenv("WEBULL_APP_SECRET", "test-secret")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "result": True,
            "data": {
                "symbol": "TSLA", "latestPrice": 240.5,
                "quotes": {"bidPrice": 240.5, "askPrice": 240.6},
            },
        })
    async with httpx.AsyncClient(transport=_mock_transport(handler)) as client:
        out = await spread.poll_webull_once(client)

    assert len(out) == 1
    cached = spread.latest("TSLA")
    assert cached and cached["source"] == "webull"
    hist = store.recent_spread_ticks(pair="TSLA", limit=10)
    assert len(hist) == 1
    assert hist[0]["source"] == "webull"


# ─── check_spread_ok gate ─────────────────────────────────────────

def test_check_spread_ok_gate_disabled_returns_true(fresh_store, monkeypatch):
    monkeypatch.setenv("TRADER_SPREAD_GATE_ENABLED", "false")
    spread._cache_row({
        "ts": "2026-07-02T00:00:00+00:00", "pair": "XBTUSD",
        "lane": "crypto", "bid": 50000, "ask": 50100,
        "last": 50050, "spread_abs": 100, "spread_bps": 20.0,
        "source": "kraken",
    })
    ok, reason, bps = spread.check_spread_ok("XBTUSD", lane="crypto")
    assert ok is True
    assert reason == "gate_disabled"
    assert bps == 20.0


def test_check_spread_ok_blocks_wide_spread_when_gate_on(fresh_store, monkeypatch):
    monkeypatch.setenv("TRADER_SPREAD_GATE_ENABLED", "true")
    monkeypatch.setenv("TRADER_SPREAD_MAX_BPS", "5.0")
    spread._cache_row({
        "ts": "2026-07-02T00:00:00+00:00", "pair": "XBTUSD",
        "lane": "crypto", "bid": 50000, "ask": 50100,
        "last": 50050, "spread_abs": 100, "spread_bps": 20.0,
        "source": "kraken",
    })
    ok, reason, bps = spread.check_spread_ok("XBTUSD", lane="crypto")
    assert ok is False
    assert "spread_wide" in reason
    assert bps == 20.0


def test_check_spread_ok_fails_open_on_stale(fresh_store, monkeypatch):
    """A dead poller must not deadlock trading."""
    monkeypatch.setenv("TRADER_SPREAD_GATE_ENABLED", "true")
    monkeypatch.setenv("TRADER_SPREAD_STALE_SEC", "1")
    # No cached row at all → is_stale=True → fail-open
    ok, reason, bps = spread.check_spread_ok("XBTUSD", lane="crypto")
    assert ok is True
    assert reason == "spread_stale"
    assert bps is None


def test_check_spread_ok_equity_uses_equity_cap(fresh_store, monkeypatch):
    monkeypatch.setenv("TRADER_EQUITY_SPREAD_GATE_ENABLED", "true")
    monkeypatch.setenv("TRADER_EQUITY_SPREAD_MAX_BPS", "10.0")
    # crypto gate stays off but equity gate on
    monkeypatch.setenv("TRADER_SPREAD_GATE_ENABLED", "false")
    spread._cache_row({
        "ts": "2026-07-02T00:00:00+00:00", "pair": "TSLA",
        "lane": "equity", "bid": 240.0, "ask": 240.72,
        "last": 240.4, "spread_abs": 0.72, "spread_bps": 30.0,
        "source": "webull",
    })
    ok, reason, _bps = spread.check_spread_ok("TSLA", lane="equity")
    assert ok is False
    assert "spread_wide" in reason


# ─── risk.check integration ───────────────────────────────────────

@pytest.mark.asyncio
async def test_risk_check_blocks_crypto_on_wide_spread(fresh_store, monkeypatch):
    """End-to-end: risk.check must refuse a crypto order when the
    poller has flagged the spread as too wide and the gate is on."""
    from trader import risk, state
    # Enable master switch + lane so we reach the spread gate
    monkeypatch.setattr(state, "master_switch_armed", lambda: True)
    monkeypatch.setattr(state, "lane_enabled", lambda _lane: True)
    monkeypatch.setenv("TRADER_SPREAD_GATE_ENABLED", "true")
    monkeypatch.setenv("TRADER_SPREAD_MAX_BPS", "5.0")
    spread._cache_row({
        "ts": "2026-07-02T00:00:00+00:00", "pair": "XBTUSD",
        "lane": "crypto", "bid": 50000, "ask": 50100,
        "last": 50050, "spread_abs": 100, "spread_bps": 20.0,
        "source": "kraken",
    })
    v = await risk.check(
        None,
        {"intent_id": "test-1", "lane": "crypto", "symbol": "XBTUSD"},
        notional_usd=5.0,
    )
    assert v.ok is False
    assert "spread_wide" in v.reason


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
