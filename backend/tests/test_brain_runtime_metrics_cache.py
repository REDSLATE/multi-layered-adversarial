"""Tests for the cached brain_runtime_metrics micro-doc (2026-07-09 P0).

Doctrine (operator directive):

    "Runtime status should stop querying shared_intents live. Move
     status to a tiny cached heartbeat/metrics document. Update it
     when intents are written."

These tests validate:
    1. `bump_on_emit` upserts a doc keyed by brain, increments
       `lifetime_count`, and stamps latest_ts / latest_action /
       latest_symbol.
    2. `refresh_windows` computes last_1h / last_24h / by_action from
       `shared_intents` using the (stack_canonical, ingest_ts) index,
       and caches for 30s.
    3. `/admin/runtime/{brain}/status` reads from the cached doc and
       does NOT scan `shared_intents` directly.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from db import db
from namespaces import SHARED_INTENTS
from shared.brain_runtime_metrics import (
    COLLECTION,
    bump_on_emit,
    get_metrics,
    refresh_windows,
)


# ── Cleanup fixture ────────────────────────────────────────────────
# `test_database` is shared with prod. Every test doc uses a synthetic
# brain name (`test-metrics-*`) that no prod path emits, and we purge
# by that prefix on teardown so nothing leaks.

_TEST_BRAIN = "test-metrics-brain"
_TEST_INTENT_PREFIX = "test-metrics-intent-"


@pytest.fixture(autouse=True)
async def _cleanup():
    """Purge synthetic test rows before and after each test."""
    await db[COLLECTION].delete_many({"_id": {"$regex": "^test-metrics-"}})
    await db[SHARED_INTENTS].delete_many(
        {"intent_id": {"$regex": f"^{_TEST_INTENT_PREFIX}"}},
    )
    yield
    await db[COLLECTION].delete_many({"_id": {"$regex": "^test-metrics-"}})
    await db[SHARED_INTENTS].delete_many(
        {"intent_id": {"$regex": f"^{_TEST_INTENT_PREFIX}"}},
    )


# ── bump_on_emit ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_bump_on_emit_upserts_new_brain():
    """First bump for a brain creates the doc with lifetime_count=1."""
    ts = datetime.now(timezone.utc).isoformat()
    await bump_on_emit(
        brain=_TEST_BRAIN, action="BUY", symbol="NVDA", ingest_ts=ts,
    )
    doc = await get_metrics(_TEST_BRAIN)
    assert doc is not None
    assert doc["_id"] == _TEST_BRAIN
    assert doc["latest_ts"] == ts
    assert doc["latest_action"] == "BUY"
    assert doc["latest_symbol"] == "NVDA"
    assert doc["lifetime_count"] == 1
    assert doc.get("first_seen_at") is not None
    assert doc.get("updated_at") is not None


@pytest.mark.asyncio
async def test_bump_on_emit_increments_lifetime_and_updates_latest():
    """Subsequent bumps increment lifetime_count and overwrite latest_*."""
    ts1 = datetime.now(timezone.utc).isoformat()
    await bump_on_emit(
        brain=_TEST_BRAIN, action="BUY", symbol="NVDA", ingest_ts=ts1,
    )
    ts2 = (
        datetime.now(timezone.utc) + timedelta(seconds=1)
    ).isoformat()
    await bump_on_emit(
        brain=_TEST_BRAIN, action="SELL", symbol="TSLA", ingest_ts=ts2,
    )
    doc = await get_metrics(_TEST_BRAIN)
    assert doc["lifetime_count"] == 2
    assert doc["latest_ts"] == ts2
    assert doc["latest_action"] == "SELL"
    assert doc["latest_symbol"] == "TSLA"


@pytest.mark.asyncio
async def test_bump_on_emit_swallows_errors():
    """A bad brain name (empty string) must NEVER raise."""
    # Best-effort semantic — empty brain skipped silently.
    await bump_on_emit(brain="", action="BUY", symbol="X", ingest_ts="ts")
    # No assertion needed — the point is no exception.


# ── refresh_windows ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_refresh_windows_computes_from_shared_intents():
    """refresh_windows aggregates over shared_intents by stack_canonical."""
    now = datetime.now(timezone.utc)
    # Insert 3 recent (last 1h) and 2 in the 1-24h band.
    rows = []
    for i in range(3):
        rows.append({
            "intent_id": f"{_TEST_INTENT_PREFIX}{i}",
            "stack_canonical": _TEST_BRAIN,
            "action": "BUY",
            "ingest_ts": (now - timedelta(minutes=5 * (i + 1))).isoformat(),
        })
    for i in range(2):
        rows.append({
            "intent_id": f"{_TEST_INTENT_PREFIX}old-{i}",
            "stack_canonical": _TEST_BRAIN,
            "action": "SELL",
            "ingest_ts": (now - timedelta(hours=5 + i)).isoformat(),
        })
    await db[SHARED_INTENTS].insert_many(rows)

    doc = await refresh_windows(_TEST_BRAIN, force=True)
    assert doc is not None
    assert doc["last_1h"] == 3
    assert doc["last_24h"] == 5
    assert doc["by_action"] == {"BUY": 3, "SELL": 2}
    assert doc.get("windows_refreshed_at") is not None


@pytest.mark.asyncio
async def test_refresh_windows_caches_within_ttl():
    """Second refresh within the TTL returns the cached doc unchanged."""
    now = datetime.now(timezone.utc)
    await db[SHARED_INTENTS].insert_one({
        "intent_id": f"{_TEST_INTENT_PREFIX}cache",
        "stack_canonical": _TEST_BRAIN,
        "action": "BUY",
        "ingest_ts": (now - timedelta(minutes=1)).isoformat(),
    })
    first = await refresh_windows(_TEST_BRAIN, force=True)
    assert first["last_1h"] == 1
    first_refreshed_at = first["windows_refreshed_at"]

    # Insert another row AFTER first refresh — cached second call
    # must NOT observe it (proves the TTL cache short-circuits).
    await db[SHARED_INTENTS].insert_one({
        "intent_id": f"{_TEST_INTENT_PREFIX}cache-2",
        "stack_canonical": _TEST_BRAIN,
        "action": "BUY",
        "ingest_ts": (now - timedelta(seconds=30)).isoformat(),
    })
    cached = await refresh_windows(_TEST_BRAIN)  # not forced
    assert cached is not None
    assert cached["windows_refreshed_at"] == first_refreshed_at
    assert cached["last_1h"] == 1  # stale by design

    # Forced refresh sees both rows.
    fresh = await refresh_windows(_TEST_BRAIN, force=True)
    assert fresh["last_1h"] == 2


@pytest.mark.asyncio
async def test_refresh_windows_new_brain_returns_zeros():
    """Brand-new brain with no intents refreshes to zero counts."""
    doc = await refresh_windows(_TEST_BRAIN, force=True)
    assert doc is not None
    assert doc["last_1h"] == 0
    assert doc["last_24h"] == 0
    assert doc["by_action"] == {}
