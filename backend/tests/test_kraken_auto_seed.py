"""Kraken pair-floor auto-seeder — safety contract (2026-02-17).

Locks in the doctrine:
    Kraken official ordermin × live mid = computed floor
    operator-set explicit floor ALWAYS wins  (skipped by seeder)
    auto-seed only missing/non-overridden pairs
    NEVER block trading if Kraken API fails
    every seeded row carries source metadata
        (source, ordermin, mid_price, kraken_pair_code, operator_override=false)
    XBT/USD → BTC/USD  (canonicalization guard)
"""
from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, "/app")
sys.path.insert(0, "/app/backend")


# ─── Canonicalization ────────────────────────────────────────────────

def test_canonicalize_xbt_becomes_btc():
    from shared.kraken_auto_seed import _canonicalize_wsname
    assert _canonicalize_wsname("XBT/USD") == "BTC/USD"


def test_canonicalize_leaves_other_pairs_untouched():
    from shared.kraken_auto_seed import _canonicalize_wsname
    assert _canonicalize_wsname("ETH/USD") == "ETH/USD"
    assert _canonicalize_wsname("SOL/USD") == "SOL/USD"


def test_canonicalize_empty_stays_empty():
    from shared.kraken_auto_seed import _canonicalize_wsname
    assert _canonicalize_wsname("") == ""
    assert _canonicalize_wsname(None) == ""


# ─── Never-block-on-API-failure ─────────────────────────────────────

@pytest.mark.asyncio
async def test_run_once_returns_gracefully_when_kraken_api_unreachable():
    """Doctrine: NEVER block trading if Kraken API fails."""
    from shared import kraken_auto_seed as m

    with patch.object(m, "_fetch_asset_pairs", AsyncMock(return_value={})):
        out = await m.run_once()
    assert out["ok"] is False
    assert out["reason"] == "kraken_api_unreachable"
    assert out["seeded"] == 0


# ─── Operator-override wins ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_seeder_skips_operator_override_rows():
    """Doctrine: operator-set floors ALWAYS win — the seeder MUST
    skip any row with `operator_override=true` regardless of what
    Kraken says the ordermin is."""
    from shared import kraken_auto_seed as m

    fake_pairs = {
        "XETHZUSD": {"wsname": "ETH/USD", "ordermin": "0.001"},
    }
    fake_ticks = {"XETHZUSD": 5000.0}
    writes = []

    async def _find_one(query, projection=None):  # noqa: ARG001
        # Simulate an existing operator-set row.
        return {"_id": "ETH/USD", "operator_override": True}

    async def _update_one(*a, **k):
        writes.append({"args": a, "kwargs": k})

    coll = MagicMock()
    coll.find_one = _find_one
    coll.update_one = _update_one

    fake_db = MagicMock()
    fake_db.__getitem__ = MagicMock(return_value=coll)

    with patch.object(m, "db", fake_db), \
         patch.object(m, "_fetch_asset_pairs", AsyncMock(return_value=fake_pairs)), \
         patch.object(m, "_fetch_tickers", AsyncMock(return_value=fake_ticks)), \
         patch.object(m, "invalidate_cache", MagicMock()):
        out = await m.run_once()

    assert out["ok"] is True
    assert out["seeded"] == 0
    assert out["skipped_operator_override"] == 1
    assert not writes, "Operator-override row must not be written to"


# ─── Happy path — seed a fresh pair with full metadata ──────────────

@pytest.mark.asyncio
async def test_seeder_writes_full_source_metadata():
    """Every seeded row MUST carry: pair, min_notional_usd, source,
    ordermin, mid_price, kraken_pair_code, updated_at,
    operator_override=false."""
    from shared import kraken_auto_seed as m

    fake_pairs = {
        "XETHZUSD": {"wsname": "ETH/USD", "ordermin": "0.001"},
    }
    fake_ticks = {"XETHZUSD": 5120.0}
    writes = []

    async def _find_one(query, projection=None):  # noqa: ARG001
        return None  # no existing row → seed

    async def _update_one(query, update, upsert=False):  # noqa: ARG001
        writes.append({"query": query, "update": update, "upsert": upsert})

    coll = MagicMock()
    coll.find_one = _find_one
    coll.update_one = _update_one

    fake_db = MagicMock()
    fake_db.__getitem__ = MagicMock(return_value=coll)

    with patch.object(m, "db", fake_db), \
         patch.object(m, "_fetch_asset_pairs", AsyncMock(return_value=fake_pairs)), \
         patch.object(m, "_fetch_tickers", AsyncMock(return_value=fake_ticks)), \
         patch.object(m, "invalidate_cache", MagicMock()):
        out = await m.run_once()

    assert out["ok"] is True
    assert out["seeded"] == 1
    assert len(writes) == 1
    w = writes[0]
    assert w["query"] == {"_id": "ETH/USD"}
    assert w["upsert"] is True
    set_doc = w["update"]["$set"]
    assert set_doc["min_notional_usd"] == round(0.001 * 5120.0, 4)  # 5.12
    assert set_doc["policy"] == "size_up"
    assert set_doc["source"] == "kraken_auto_seed"
    assert set_doc["ordermin"] == "0.001"
    assert set_doc["mid_price"] == 5120.0
    assert set_doc["kraken_pair_code"] == "XETHZUSD"
    assert set_doc["operator_override"] is False
    assert "updated_at" in set_doc


@pytest.mark.asyncio
async def test_seeder_canonicalizes_xbt_to_btc_when_writing():
    """The seeded doc `_id` MUST be `BTC/USD`, not `XBT/USD`, so it
    matches our canonical intent symbol."""
    from shared import kraken_auto_seed as m

    fake_pairs = {
        "XXBTZUSD": {"wsname": "XBT/USD", "ordermin": "0.00005"},
    }
    fake_ticks = {"XXBTZUSD": 62000.0}
    writes = []

    async def _find_one(q, projection=None): return None  # noqa: ARG001
    async def _update_one(q, u, upsert=False):
        writes.append({"query": q, "update": u})

    coll = MagicMock()
    coll.find_one = _find_one
    coll.update_one = _update_one
    fake_db = MagicMock()
    fake_db.__getitem__ = MagicMock(return_value=coll)

    with patch.object(m, "db", fake_db), \
         patch.object(m, "_fetch_asset_pairs", AsyncMock(return_value=fake_pairs)), \
         patch.object(m, "_fetch_tickers", AsyncMock(return_value=fake_ticks)), \
         patch.object(m, "invalidate_cache", MagicMock()):
        await m.run_once()

    assert writes[0]["query"] == {"_id": "BTC/USD"}
    assert writes[0]["update"]["$set"]["kraken_pair_code"] == "XXBTZUSD"


# ─── Skip pairs where the ticker didn't come back ───────────────────

@pytest.mark.asyncio
async def test_seeder_skips_pairs_without_mid_price():
    """If AssetPairs returns a pair but Ticker doesn't, we can't
    compute a floor — SKIP that pair, don't halve-write it."""
    from shared import kraken_auto_seed as m

    fake_pairs = {
        "XETHZUSD": {"wsname": "ETH/USD", "ordermin": "0.001"},
        "XXBTZUSD": {"wsname": "XBT/USD", "ordermin": "0.00005"},
    }
    fake_ticks = {"XETHZUSD": 5000.0}  # BTC ticker missing
    writes = []

    async def _find_one(q, projection=None): return None  # noqa: ARG001
    async def _update_one(q, u, upsert=False):
        writes.append(q)

    coll = MagicMock()
    coll.find_one = _find_one
    coll.update_one = _update_one
    fake_db = MagicMock()
    fake_db.__getitem__ = MagicMock(return_value=coll)

    with patch.object(m, "db", fake_db), \
         patch.object(m, "_fetch_asset_pairs", AsyncMock(return_value=fake_pairs)), \
         patch.object(m, "_fetch_tickers", AsyncMock(return_value=fake_ticks)), \
         patch.object(m, "invalidate_cache", MagicMock()):
        out = await m.run_once()

    assert out["seeded"] == 1
    assert out["skipped_no_mid"] == 1
    assert writes == [{"_id": "ETH/USD"}]  # BTC skipped


# ─── Cache invalidation after write ─────────────────────────────────


@pytest.mark.asyncio
async def test_seeder_invalidates_pair_floor_cache_after_writes():
    """After the seeder writes any floor, the auto-router's pair-floor
    cache MUST be invalidated so the next tick sees the fresh row."""
    from shared import kraken_auto_seed as m

    fake_pairs = {"XETHZUSD": {"wsname": "ETH/USD", "ordermin": "0.001"}}
    fake_ticks = {"XETHZUSD": 5000.0}

    async def _find_one(q, projection=None): return None  # noqa: ARG001
    async def _update_one(*a, **k): pass

    coll = MagicMock()
    coll.find_one = _find_one
    coll.update_one = _update_one
    fake_db = MagicMock()
    fake_db.__getitem__ = MagicMock(return_value=coll)
    invalidate_mock = MagicMock()

    with patch.object(m, "db", fake_db), \
         patch.object(m, "_fetch_asset_pairs", AsyncMock(return_value=fake_pairs)), \
         patch.object(m, "_fetch_tickers", AsyncMock(return_value=fake_ticks)), \
         patch.object(m, "invalidate_cache", invalidate_mock):
        await m.run_once()

    assert invalidate_mock.called, (
        "kraken_auto_seed must invalidate the pair-floor cache after "
        "writing so the auto-router picks up new floors on next tick."
    )
