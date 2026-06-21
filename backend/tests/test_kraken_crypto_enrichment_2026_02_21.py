"""Tests for Kraken-first crypto spread enrichment (2026-02-21).

Two layers covered:
  1. `kraken_bidask()` — the public-ticker fetcher itself. We mock
     httpx so the unit tests don't touch the live Kraken API.
  2. `enrich_crypto_doctrine_snapshot()` — the wired flow. We assert
     that when Kraken returns valid bid/ask, the resulting snapshot
     replaces the Webull spread block and tags `primary_source =
     "kraken"`. We also assert the fail-soft path (Kraken returns
     None → Webull spread untouched).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.snapshot_enrich import crypto_doctrine, kraken_feed


# ── kraken_feed.kraken_bidask ────────────────────────────────────────

@pytest.fixture(autouse=True)
def _clear_caches():
    """Wipe both module-level caches between tests for determinism."""
    kraken_feed._cache.clear()
    crypto_doctrine._BROKER_SEL_CACHE["value"] = None
    crypto_doctrine._BROKER_SEL_CACHE["fetched_at"] = 0.0
    yield
    kraken_feed._cache.clear()
    crypto_doctrine._BROKER_SEL_CACHE["value"] = None
    crypto_doctrine._BROKER_SEL_CACHE["fetched_at"] = 0.0


def _kraken_response(bid: str, ask: str, last: str):
    """Build a minimal Kraken public/Ticker response payload."""
    return {
        "error": [],
        "result": {
            "XETHZUSD": {
                "a": [ask, "1", "1.0"],
                "b": [bid, "1", "1.0"],
                "c": [last, "0.1"],
                "v": ["100", "200"],
            }
        },
    }


def _httpx_mock(json_payload, status_code: int = 200):
    """Return a mock httpx.AsyncClient that yields the given JSON."""
    response = MagicMock()
    response.json = MagicMock(return_value=json_payload)
    response.raise_for_status = MagicMock(return_value=None)
    response.status_code = status_code

    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    client.get = AsyncMock(return_value=response)
    return client


@pytest.mark.asyncio
async def test_kraken_bidask_happy_path():
    payload = _kraken_response(bid="2500.10", ask="2500.20", last="2500.15")
    with patch.object(kraken_feed.httpx, "AsyncClient",
                      return_value=_httpx_mock(payload)):
        out = await kraken_feed.kraken_bidask("ETH/USD")
    assert out is not None
    assert out["bid"] == 2500.10
    assert out["ask"] == 2500.20
    assert out["price"] == 2500.15
    assert out["src"] == "kraken"
    # spread = (0.10 / 2500.15) * 10000 ≈ 0.40 bps
    assert 0.3 < out["spread_bps"] < 0.5


@pytest.mark.asyncio
async def test_kraken_bidask_returns_none_on_empty_result():
    payload = {"error": [], "result": {}}
    with patch.object(kraken_feed.httpx, "AsyncClient",
                      return_value=_httpx_mock(payload)):
        out = await kraken_feed.kraken_bidask("ETH/USD")
    assert out is None


@pytest.mark.asyncio
async def test_kraken_bidask_returns_none_on_kraken_error():
    payload = {"error": ["EService:Unavailable"], "result": {}}
    with patch.object(kraken_feed.httpx, "AsyncClient",
                      return_value=_httpx_mock(payload)):
        out = await kraken_feed.kraken_bidask("ETH/USD")
    assert out is None


@pytest.mark.asyncio
async def test_kraken_bidask_returns_none_on_missing_fields():
    payload = {
        "error": [],
        "result": {"XETHZUSD": {"c": ["2500.15", "0.1"]}},  # no a/b
    }
    with patch.object(kraken_feed.httpx, "AsyncClient",
                      return_value=_httpx_mock(payload)):
        out = await kraken_feed.kraken_bidask("ETH/USD")
    assert out is None


@pytest.mark.asyncio
async def test_kraken_bidask_caches_within_ttl():
    payload = _kraken_response(bid="2500.10", ask="2500.20", last="2500.15")
    mock_client = _httpx_mock(payload)
    with patch.object(kraken_feed.httpx, "AsyncClient",
                      return_value=mock_client):
        out1 = await kraken_feed.kraken_bidask("ETH/USD")
        out2 = await kraken_feed.kraken_bidask("ETH/USD")
    assert out1 == out2
    # Only one HTTP call should have fired thanks to the 5s cache.
    assert mock_client.get.call_count == 1


@pytest.mark.asyncio
async def test_kraken_bidask_returns_none_on_exception():
    bad_client = MagicMock()
    bad_client.__aenter__ = AsyncMock(side_effect=RuntimeError("boom"))
    bad_client.__aexit__ = AsyncMock(return_value=None)
    with patch.object(kraken_feed.httpx, "AsyncClient",
                      return_value=bad_client):
        out = await kraken_feed.kraken_bidask("ETH/USD")
    assert out is None


# ── crypto_doctrine wiring ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_crypto_doctrine_uses_kraken_when_primary():
    """When broker_selection.crypto = "kraken" AND Kraken returns
    valid bid/ask, the enriched snapshot must carry Kraken's spread
    and tag primary_source = "kraken"."""
    kraken_payload = {
        "bid": 2500.10, "ask": 2500.20, "price": 2500.15,
        "spread_bps": 0.40, "src": "kraken",
    }
    base = {"symbol": "ETH/USD", "lane": "crypto"}

    async def fake_kraken(*_args, **_kw):
        return kraken_payload

    async def fake_primary():
        return True

    # Stub the sync executor so we don't hit Webull.
    def fake_sync(symbol, base_in):
        out = dict(base_in)
        out["symbol"] = symbol
        out["price"] = 2499.0
        out["bid"] = None
        out["ask"] = None
        out["spread_bps"] = 30.0
        out["spread_bps_source"] = "default_fallback_missing_bidask"
        out["primary_source"] = "webull"
        out["snapshot_source"] = "webull"
        out["webull_enriched"] = True
        out["data_council"] = ["webull"]
        return out

    with patch.object(crypto_doctrine, "_crypto_primary_is_kraken", fake_primary), \
         patch("shared.snapshot_enrich.kraken_feed.kraken_bidask", fake_kraken), \
         patch.object(crypto_doctrine, "_enrich_sync", fake_sync):
        out = await crypto_doctrine.enrich_crypto_doctrine_snapshot("ETH/USD", base)

    assert out["bid"] == 2500.10
    assert out["ask"] == 2500.20
    assert out["spread_bps"] == 0.40
    assert out["primary_source"] == "kraken"
    assert out["snapshot_source"] == "kraken"
    assert out["spread_bps_source"] == "kraken_public_ticker"
    assert out["kraken_enriched"] is True
    assert "kraken" in out["data_council"]


@pytest.mark.asyncio
async def test_crypto_doctrine_falls_through_when_kraken_returns_none():
    """When Kraken returns nothing, the Webull-derived spread must
    remain untouched (fail-soft)."""
    base = {"symbol": "ETH/USD", "lane": "crypto"}

    async def fake_kraken(*_args, **_kw):
        return None

    async def fake_primary():
        return True

    def fake_sync(symbol, base_in):
        out = dict(base_in)
        out["spread_bps"] = 30.0
        out["primary_source"] = "webull"
        return out

    with patch.object(crypto_doctrine, "_crypto_primary_is_kraken", fake_primary), \
         patch("shared.snapshot_enrich.kraken_feed.kraken_bidask", fake_kraken), \
         patch.object(crypto_doctrine, "_enrich_sync", fake_sync):
        out = await crypto_doctrine.enrich_crypto_doctrine_snapshot("ETH/USD", base)

    assert out["spread_bps"] == 30.0
    assert out["primary_source"] == "webull"
    assert "kraken_enriched" not in out


@pytest.mark.asyncio
async def test_crypto_doctrine_skips_kraken_when_selection_is_webull():
    """When broker_selection says crypto = "webull", we MUST NOT call
    Kraken (preserves the operator's explicit choice)."""
    base = {"symbol": "ETH/USD", "lane": "crypto"}

    async def fake_primary():
        return False

    def fake_sync(symbol, base_in):
        out = dict(base_in)
        out["spread_bps"] = 30.0
        out["primary_source"] = "webull"
        return out

    kraken_call_count = {"n": 0}

    async def fake_kraken(*_args, **_kw):
        kraken_call_count["n"] += 1
        return None

    with patch.object(crypto_doctrine, "_crypto_primary_is_kraken", fake_primary), \
         patch("shared.snapshot_enrich.kraken_feed.kraken_bidask", fake_kraken), \
         patch.object(crypto_doctrine, "_enrich_sync", fake_sync):
        out = await crypto_doctrine.enrich_crypto_doctrine_snapshot("ETH/USD", base)

    assert kraken_call_count["n"] == 0
    assert out["primary_source"] == "webull"
