"""Kraken pair auto-sync + affordability (2026-07-22)."""
from __future__ import annotations

import sys
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, "/app/backend")

from shared.crypto import kraken_pair_sync as kps


def _pairs():
    return {
        "BTC": {"pair": "XXBTZUSD", "ordermin": 0.00005},
        "YGG": {"pair": "YGGUSD", "ordermin": 200.0},
        "SIDEKICK": {"pair": "SIDEKICKUSD", "ordermin": 50000.0},
        "TRAC": {"pair": "TRACUSD", "ordermin": 20.0},
    }


# ── AssetPairs parsing (operator spec) ──────────────────────────────

@pytest.mark.asyncio
async def test_usd_pairs_filters_offline_and_darkpool():
    body = {"error": [], "result": {
        "XXBTZUSD": {"wsname": "XBT/USD", "status": "online",
                     "altname": "XBTUSD", "ordermin": "0.00005"},
        "DARKUSD": {"wsname": "DARK/USD", "status": "online",
                    "altname": "DARKUSD.d", "ordermin": "1"},
        "GONEUSD": {"wsname": "GONE/USD", "status": "delisted",
                    "altname": "GONEUSD", "ordermin": "1"},
        "ETHEUR": {"wsname": "ETH/EUR", "status": "online",
                   "altname": "ETHEUR", "ordermin": "0.01"},
        "SOLUSD": {"wsname": "SOL/USD", "status": "online",
                   "altname": "SOLUSD", "ordermin": "0.1"},
    }}

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return body

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            return _Resp()

    kps._cache.update(at=0.0, pairs=None)
    with patch.object(kps.httpx, "AsyncClient", return_value=_Client()):
        pairs = await kps.get_usd_pairs(force=True)
    assert set(pairs) == {"BTC", "SOL"}  # XBT aliased; dark/delisted/EUR out
    assert pairs["BTC"]["pair"] == "XXBTZUSD"
    kps._cache.update(at=0.0, pairs=None)


# ── auto-map ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_auto_map_upserts_only_kraken_tradable():
    from db import db
    for cid in ("CRYPTO:TRAC-USD", "CRYPTO:NOTREAL-USD"):
        await db[kps.OVERRIDES].delete_one({"_id": cid})
    try:
        with patch.object(kps, "get_usd_pairs", new=AsyncMock(return_value=_pairs())):
            res = await kps.auto_map_symbols(["TRAC/USD", "NOTREAL/USD", "BTC/USD"])
        assert "TRAC/USD" in res["mapped"]
        assert "NOTREAL/USD" in res["not_on_kraken"]
        assert "BTC/USD" not in res["mapped"]  # already static-mapped
        doc = await db[kps.OVERRIDES].find_one({"_id": "CRYPTO:TRAC-USD"})
        assert doc and doc["kraken_pair"] == "TRACUSD"
        assert doc["source"] == "auto_sync"
        from shared.broker_symbol_resolver import has_kraken_mapping
        assert has_kraken_mapping("CRYPTO:TRAC-USD")
    finally:
        await db[kps.OVERRIDES].delete_one({"_id": "CRYPTO:TRAC-USD"})
        from shared.broker_symbol_resolver import ensure_kraken_overrides_fresh
        await ensure_kraken_overrides_fresh(force=True)


@pytest.mark.asyncio
async def test_auto_map_fail_soft_when_assetpairs_down():
    with patch.object(kps, "get_usd_pairs", new=AsyncMock(return_value=None)):
        res = await kps.auto_map_symbols(["ZZZZ/USD"])
    assert res["mapped"] == []
    assert res.get("error") == "assetpairs_unavailable"


# ── affordability ───────────────────────────────────────────────────

def _row(sym, price, pinned=False):
    return {"canonical_symbol": sym, "price": price, "pinned": pinned}


@pytest.mark.asyncio
async def test_unaffordable_ordermin_dropped():
    rows = [
        _row("SIDEKICK/USD", 0.0008),  # 50000 × 0.0008 = $40 > $10 cap
        _row("TRAC/USD", 0.30),        # 20 × 0.30 = $6 ≤ $10
        _row("BTC/USD", 100000.0),     # 0.00005 × 100000 = $5 ≤ $10
    ]
    with patch.object(kps, "get_usd_pairs", new=AsyncMock(return_value=_pairs())):
        kept, dropped = await kps.filter_affordable(rows, 10.0)
    assert [r["canonical_symbol"] for r in kept] == ["TRAC/USD", "BTC/USD"]
    assert dropped[0]["_drop_reason"] == "unaffordable_ordermin"
    assert dropped[0]["_min_notional_usd"] == 40.0


@pytest.mark.asyncio
async def test_pins_exempt_from_affordability():
    rows = [_row("SIDEKICK/USD", 0.0008, pinned=True)]
    with patch.object(kps, "get_usd_pairs", new=AsyncMock(return_value=_pairs())):
        kept, dropped = await kps.filter_affordable(rows, 10.0)
    assert kept and not dropped


@pytest.mark.asyncio
async def test_affordability_fail_soft_when_assetpairs_down():
    rows = [_row("SIDEKICK/USD", 0.0008)]
    with patch.object(kps, "get_usd_pairs", new=AsyncMock(return_value=None)):
        kept, dropped = await kps.filter_affordable(rows, 10.0)
    assert kept == rows and dropped == []
