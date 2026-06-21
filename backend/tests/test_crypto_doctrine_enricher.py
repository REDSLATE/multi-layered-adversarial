"""Crypto doctrine enricher — Webull as hot-failover for Kraken."""
from __future__ import annotations

import asyncio
import pytest

from shared.snapshot_enrich import crypto_doctrine as cd


@pytest.fixture(autouse=True)
def _mock_kraken_off(monkeypatch):
    """Pin tests to the Webull-only path (Kraken-first is covered in
    test_kraken_crypto_enrichment_2026_02_21.py). Returning None from
    kraken_bidask + selection-is-not-kraken keeps these legacy tests
    exercising the original Webull enrichment they were written for.
    """
    async def _no_kraken(*_a, **_k):
        return None

    async def _not_primary():
        return False

    monkeypatch.setattr(
        "shared.snapshot_enrich.kraken_feed.kraken_bidask", _no_kraken,
    )
    monkeypatch.setattr(cd, "_crypto_primary_is_kraken", _not_primary)


class _FakeClient:
    def __init__(self, crypto_snap=None):
        self._snap = crypto_snap

    def crypto_snapshot(self, sym):
        return self._snap

    # Stubs for parts of the interface the equity enricher uses but
    # the crypto one ignores. Kept here so the singleton replacement
    # doesn't blow up if other code paths probe.
    def equity_snapshot(self, sym):
        return None

    def instrument(self, sym):
        return None

    def equity_bars(self, *_a, **_k):
        return []

    def most_active_map(self):
        return {}


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_returns_base_when_client_missing(monkeypatch):
    """When the Webull client is unavailable, the enricher must NOT
    invent market data. It may stamp audit-metadata fields
    (`snapshot_source`, `snapshot_age_ms`) so the operator can see
    *why* the snapshot is bare, but it must NOT inject price / bid /
    ask / spread_bps."""
    monkeypatch.setattr(
        "shared.market_data.webull_quotes.get_quotes_client",
        lambda: None,
    )
    base = {"symbol": "BTC/USD", "lane": "crypto"}
    out = _run(cd.enrich_crypto_doctrine_snapshot("BTC/USD", base))
    # Base fields preserved unchanged.
    assert out["symbol"] == "BTC/USD"
    assert out["lane"] == "crypto"
    # No fabricated market-data fields.
    for forbidden in ("price", "bid", "ask", "spread_bps", "volume"):
        assert forbidden not in out, (
            f"enricher fabricated {forbidden!r} with no upstream client"
        )
    # Audit metadata is allowed (and useful) — but the source tag
    # must make the missing client obvious.
    assert out.get("snapshot_source") == "base_only_no_webull_client"


def test_btc_usd_enriched_with_webull_snapshot(monkeypatch):
    snap = {
        "price": 61800.0,
        "pre_close": 60000.0,
        "bid": 61750.0,
        "ask": 61850.0,
        "volume": 12_000,
        "high": 62100.0,
        "low": 60500.0,
    }
    monkeypatch.setattr(
        "shared.market_data.webull_quotes.get_quotes_client",
        lambda: _FakeClient(crypto_snap=snap),
    )
    base = {"symbol": "BTC/USD", "lane": "crypto"}
    out = _run(cd.enrich_crypto_doctrine_snapshot("BTC/USD", base))
    assert out["price"] == 61800.0
    assert out["pre_close"] == 60000.0
    assert out["gap_pct"] == pytest.approx(3.0, abs=0.01)
    assert out["spread_bps"] is not None
    assert 14 <= out["spread_bps"] <= 18  # ~16 bps for $100 spread on $61800
    assert out["webull_enriched"] is True
    assert out["primary_source"] == "webull"
    assert "webull" in out["data_council"]


def test_canonical_to_webull_pair_conversion():
    assert cd._canonical_to_webull_pair("BTC/USD") == "BTCUSD"
    assert cd._canonical_to_webull_pair("BTC-USD") == "BTCUSD"
    assert cd._canonical_to_webull_pair("ETHUSD") == "ETHUSD"
    assert cd._canonical_to_webull_pair("") == ""


def test_webull_offline_tags_council(monkeypatch):
    monkeypatch.setattr(
        "shared.market_data.webull_quotes.get_quotes_client",
        lambda: _FakeClient(crypto_snap=None),  # snapshot returns None
    )
    base = {"symbol": "BTC/USD", "lane": "crypto"}
    out = _run(cd.enrich_crypto_doctrine_snapshot("BTC/USD", base))
    # Should not have webull_enriched flag, council shows offline
    assert out.get("webull_enriched") is None
    assert "webull_offline" in out.get("data_council", [])


def test_enricher_fail_soft_on_exception(monkeypatch):
    class _Boom:
        def crypto_snapshot(self, *a, **k):
            raise RuntimeError("boom")

    monkeypatch.setattr(
        "shared.market_data.webull_quotes.get_quotes_client",
        lambda: _Boom(),
    )
    base = {"symbol": "BTC/USD", "lane": "crypto"}
    out = _run(cd.enrich_crypto_doctrine_snapshot("BTC/USD", base))
    # Original base is returned on exception — async wrapper catches
    assert "symbol" in out
