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
def fresh_store(tmp_path, monkeypatch):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    store.init(
        str(tmp_path / "executions.sqlite"),
        str(tmp_path / "jsonl"),
    )
    # Isolate the persisted Webull token per-test so live-run tokens
    # from `/app/trader/data/` don't leak into unit assertions.
    monkeypatch.setenv("WEBULL_TOKEN_PATH", str(tmp_path / "webull_token.json"))
    from trader import webull_auth as _wa
    _wa._cache = None
    # Clear the in-memory cache between tests
    spread._latest.clear()
    yield
    _wa._cache = None
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

def test_webull_sign_matches_official_sdk_formula():
    """Reproduce the exact algo from webull-inc/openapi-python-sdk:
      sign_params sorted (lowercased keys) → URI + '&' + kv-joined →
      URL-encode(safe='') → HMAC-SHA1(secret+'&', encoded) → base64.
    """
    import hmac
    import hashlib
    import base64
    import urllib.parse
    key = "dk_app_key_123"
    secret = "your_app_secret_here"
    ts = "2026-07-01T16:00:00Z"
    nonce = "abc-999-noise"
    host = "api.webull.com"
    path = "/openapi/assets/balance"
    # Manually build the expected signature exactly as the SDK does
    sp = {
        "host": host,
        "x-app-key": key,
        "x-signature-algorithm": "HMAC-SHA1",
        "x-signature-nonce": nonce,
        "x-signature-version": "1.0",
        "x-timestamp": ts,
    }
    pairs = [f"{k}={sp[k]}" for k in sorted(sp.keys())]
    stt = path + "&" + "&".join(pairs)
    encoded = urllib.parse.quote(stt, safe="")
    expected = base64.b64encode(
        hmac.new((secret + "&").encode(), encoded.encode(), hashlib.sha1).digest()
    ).decode().strip()
    got = spread._webull_sign(
        app_key=key, app_secret=secret, timestamp=ts, nonce=nonce,
        method="GET", path=path, host=host,
    )
    assert got == expected


def test_webull_sign_incorporates_query_and_body():
    """Query params + body md5 must be woven into the signature."""
    a = spread._webull_sign(
        app_key="k", app_secret="s", timestamp="T", nonce="N",
        method="GET", path="/x", host="h",
    )
    b = spread._webull_sign(
        app_key="k", app_secret="s", timestamp="T", nonce="N",
        method="GET", path="/x", host="h",
        query={"symbols": "AAPL"},
    )
    c = spread._webull_sign(
        app_key="k", app_secret="s", timestamp="T", nonce="N",
        method="POST", path="/x", host="h",
        body='{"a":1}',
    )
    assert a != b, "query params must affect the signature"
    assert a != c, "body must affect the signature"
    assert b != c


def test_parse_webull_extracts_bid_ask_from_snapshot_array(fresh_store):
    """Docs shape: array of snapshot objects with flat bid/ask."""
    payload = [{
        "symbol": "TSLA",
        "price": "240.55",
        "bid": "240.50", "bid_size": "200",
        "ask": "240.60", "ask_size": "150",
        "open": "240.00", "close": "240.55",
    }]
    row = spread._parse_webull_snapshot("TSLA", payload)
    assert row is not None
    assert row["pair"] == "TSLA"
    assert row["lane"] == "equity"
    assert row["source"] == "webull"
    assert row["bid"] == pytest.approx(240.50)
    assert row["ask"] == pytest.approx(240.60)
    assert row["last"] == pytest.approx(240.55)


def test_parse_webull_tolerates_legacy_wrapped_shape(fresh_store):
    """Regional endpoints wrap in `data.quotes` — parser must handle."""
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
async def test_fetch_webull_sends_correct_headers(fresh_store, monkeypatch):
    """Regression guard: header set must NOT include x-app-secret
    (that caused a 401 in prod). All 8 required headers present."""
    monkeypatch.setenv("WEBULL_APP_KEY", "test-key")
    monkeypatch.setenv("WEBULL_APP_SECRET", "test-secret")
    monkeypatch.setenv("WEBULL_ACCESS_TOKEN", "test-token")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        return httpx.Response(200, json=[{
            "symbol": "TSLA", "price": "240.55",
            "bid": "240.50", "ask": "240.60",
        }])

    async with httpx.AsyncClient(transport=_mock_transport(handler)) as client:
        row = await spread.fetch_webull(client, "TSLA")

    assert row is not None
    assert row["bid"] == pytest.approx(240.50)
    assert row["ask"] == pytest.approx(240.60)
    assert "openapi/market-data/stock/snapshot" in seen["url"]
    assert "symbols=TSLA" in seen["url"]
    assert "category=US_STOCK" in seen["url"]
    # Every required header present
    for h in [
        "x-app-key", "x-timestamp",
        "x-signature-version", "x-signature-algorithm",
        "x-signature-nonce", "x-access-token", "x-version", "x-signature",
    ]:
        assert seen["headers"].get(h), f"missing required header: {h}"
    assert seen["headers"]["x-app-key"] == "test-key"
    assert seen["headers"]["x-access-token"] == "test-token"
    assert seen["headers"]["x-signature-algorithm"] == "HMAC-SHA1"
    assert seen["headers"]["x-version"] == "v2"
    # CRITICAL: x-app-secret must NOT be sent (real prod cause of 401)
    assert "x-app-secret" not in {k.lower() for k in seen["headers"].keys()}, (
        "x-app-secret must NOT be sent — Webull rejects it with 401"
    )


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
async def test_fetch_webull_missing_access_token_returns_none(fresh_store, monkeypatch):
    """Webull requires x-access-token from the 2FA flow. Skip cleanly."""
    monkeypatch.setenv("WEBULL_APP_KEY", "test-key")
    monkeypatch.setenv("WEBULL_APP_SECRET", "test-secret")
    monkeypatch.delenv("WEBULL_ACCESS_TOKEN", raising=False)

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("must not hit network when access_token is missing")

    async with httpx.AsyncClient(transport=_mock_transport(handler)) as client:
        row = await spread.fetch_webull(client, "TSLA")
    assert row is None


@pytest.mark.asyncio
async def test_fetch_webull_returns_none_on_403(fresh_store, monkeypatch):
    """Auth or entitlement failures must not crash the poller."""
    monkeypatch.setenv("WEBULL_APP_KEY", "test-key")
    monkeypatch.setenv("WEBULL_APP_SECRET", "test-secret")
    monkeypatch.setenv("WEBULL_ACCESS_TOKEN", "test-token")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={
            "error_code": "UNAUTHORIZED",
            "message": "Insufficient permission",
        })

    async with httpx.AsyncClient(transport=_mock_transport(handler)) as client:
        row = await spread.fetch_webull(client, "TSLA")
    assert row is None


def test_webull_category_defaults_to_us_stock(monkeypatch):
    monkeypatch.delenv("TRADER_EQUITY_SPREAD_ETFS", raising=False)
    assert spread._webull_category("TSLA") == "US_STOCK"


def test_webull_category_flags_etfs_from_env(monkeypatch):
    monkeypatch.setenv("TRADER_EQUITY_SPREAD_ETFS", "SPY, QQQ, IWM")
    assert spread._webull_category("QQQ") == "US_ETF"
    assert spread._webull_category("TSLA") == "US_STOCK"


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
    monkeypatch.setenv("WEBULL_ACCESS_TOKEN", "test-token")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{
            "symbol": "TSLA", "price": "240.5",
            "bid": "240.5", "ask": "240.6",
        }])
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
