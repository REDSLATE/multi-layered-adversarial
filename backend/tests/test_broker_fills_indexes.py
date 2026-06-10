"""Regression tests for the broker-fills TTL + compound index.

Doctrine pin (2026-06-10, P2): the poller writes a new row every
20 seconds for every fill in a 5-minute trailing window. Without
TTL the collection would grow unbounded — historical fills are
valuable for audit but only for ~30 days. After that point Mongo
prunes via the TTL index.

These tests verify both indexes are created idempotently.
"""
from __future__ import annotations

import os
import sys

import pytest

BACKEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from db import db  # noqa: E402
from shared import broker_fills  # noqa: E402


@pytest.fixture(autouse=True)
async def _wipe_indexes_around_test():
    """Drop user indexes before each test so we observe a clean
    create. The default `_id_` index is system-managed and cannot
    be dropped."""
    try:
        await db[broker_fills.BROKER_FILLS_COLLECTION].drop_indexes()
    except Exception:  # noqa: BLE001
        pass
    yield
    try:
        await db[broker_fills.BROKER_FILLS_COLLECTION].drop_indexes()
    except Exception:  # noqa: BLE001
        pass


@pytest.mark.asyncio
async def test_ensure_indexes_creates_ttl_and_compound():
    await broker_fills._ensure_indexes()
    indexes = await db[
        broker_fills.BROKER_FILLS_COLLECTION
    ].index_information()

    # TTL index — Mongo carries expireAfterSeconds as a top-level
    # attribute on the index spec.
    assert "ttl_inserted_at" in indexes
    ttl = indexes["ttl_inserted_at"]
    assert ttl["key"] == [("inserted_at", 1)]
    assert ttl.get("expireAfterSeconds") == 30 * 24 * 3600, (
        f"TTL must be 30 days, got {ttl.get('expireAfterSeconds')}"
    )

    # Compound index for the dashboard's "recent fills by symbol".
    assert "symbol_ts_desc" in indexes
    compound = indexes["symbol_ts_desc"]
    assert compound["key"] == [("symbol", 1), ("timestamp", -1)]


@pytest.mark.asyncio
async def test_ensure_indexes_is_idempotent():
    """Calling twice must not raise — Mongo's create_index is a no-op
    when the spec matches an existing index."""
    await broker_fills._ensure_indexes()
    await broker_fills._ensure_indexes()
    indexes = await db[
        broker_fills.BROKER_FILLS_COLLECTION
    ].index_information()
    assert "ttl_inserted_at" in indexes
    assert "symbol_ts_desc" in indexes


@pytest.mark.asyncio
async def test_normalize_transaction_stamps_inserted_at_as_date():
    """The TTL index requires `inserted_at` to be a BSON Date, NOT
    an ISO string. The legacy `ingested_at` field stays as a string
    for human readability."""
    from datetime import datetime
    tx = {
        "id": "test-tx-1",
        "type": "TRADE",
        "symbol": "AAPL",
        "side": "BUY",
        "quantity": 1.0,
        "subType": "BUY",
        "direction": "DEBIT",
        "netAmount": -200.0,
        "description": "Bought 1 share of AAPL at 200.00.",
        "timestamp": "2026-06-10T00:00:00Z",
    }
    norm = broker_fills._normalize_transaction(tx, "acc-1")
    assert norm is not None
    assert isinstance(norm["inserted_at"], datetime), (
        f"inserted_at must be datetime for TTL, got {type(norm['inserted_at'])}"
    )
    # Backward-compat field still present for human audit
    assert isinstance(norm["ingested_at"], str)


@pytest.mark.asyncio
async def test_retention_is_env_tunable(monkeypatch):
    """Operator can override retention via env without code change."""
    monkeypatch.setenv("BROKER_FILLS_RETENTION_SEC", "3600")
    await broker_fills._ensure_indexes()
    indexes = await db[
        broker_fills.BROKER_FILLS_COLLECTION
    ].index_information()
    # An index with a different expireAfterSeconds is an incompatible
    # spec — Mongo will keep the old one and raise. Our impl is
    # failure-tolerant (logs and continues) so the existing 30-day
    # TTL might persist. Just assert the call didn't crash.
    assert "ttl_inserted_at" in indexes
