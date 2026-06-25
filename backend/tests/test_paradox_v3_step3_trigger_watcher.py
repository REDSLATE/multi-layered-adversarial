"""Paradox v3 — Step 3 Trigger Watcher tests.

Pins:
  * DORMANT-by-default doctrine — `is_watcher_enabled()` returns False
    when the env var is unset/empty/"0"/false-ish.
  * `scan_watch_queue()` is a pure no-op (no DB reads) when dormant.
  * `enqueue_watch_plan()` still writes when dormant (so flag flips
    can drain backlog).
  * When LIVE: TTL-expired rows transition; price triggers / invalidations
    transition only when a `price_fetcher` is supplied.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone, timedelta

import pytest

from db import db
from namespaces import SHARED_INTENTS
from shared.pipeline.trigger_watcher import (
    INTENT_WATCH_QUEUE_COLL,
    enqueue_watch_plan,
    is_watcher_enabled,
    scan_watch_queue,
    watch_queue_snapshot,
)


pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _clear_flag(monkeypatch):
    monkeypatch.delenv("PARADOX_V3_TRIGGER_WATCHER", raising=False)
    yield


# ── Flag semantics ────────────────────────────────────────────────
def test_dormant_by_default():
    assert is_watcher_enabled() is False


@pytest.mark.parametrize("val", ["", "0", "false", "no", "off", "  "])
def test_flag_off_for_falsey_values(monkeypatch, val):
    monkeypatch.setenv("PARADOX_V3_TRIGGER_WATCHER", val)
    assert is_watcher_enabled() is False


@pytest.mark.parametrize("val", ["1", "true", "yes", "on", "TRUE", "On"])
def test_flag_on_for_truthy_values(monkeypatch, val):
    monkeypatch.setenv("PARADOX_V3_TRIGGER_WATCHER", val)
    assert is_watcher_enabled() is True


# ── Dormant scan does not touch Mongo ─────────────────────────────
async def test_scan_returns_zero_counters_when_dormant():
    out = await scan_watch_queue()
    assert out == {
        "enabled": False,
        "scanned": 0,
        "fired": 0,
        "invalidated": 0,
        "expired": 0,
    }


# ── Enqueue still works even when dormant ─────────────────────────
async def test_enqueue_writes_when_dormant():
    iid = f"t-enqueue-{uuid.uuid4().hex[:10]}"
    # Seed a minimal intent row so the gate_state update target exists.
    await db[SHARED_INTENTS].insert_one({"intent_id": iid, "gate_state": "pending"})
    try:
        row = await enqueue_watch_plan(
            intent_id=iid,
            symbol="NVDA",
            lane="equity",
            stance="BULLISH",
            trigger_price=187.40,
            invalidation_price=184.20,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=6),
        )
        assert row["state"] == "watching"
        stored = await db[INTENT_WATCH_QUEUE_COLL].find_one({"intent_id": iid})
        assert stored is not None
        assert stored["stance"] == "BULLISH"
        # Intent doc gate_state was stamped.
        upd = await db[SHARED_INTENTS].find_one({"intent_id": iid})
        assert upd["gate_state"] == "waiting_for_trigger"
    finally:
        await db[INTENT_WATCH_QUEUE_COLL].delete_many({"intent_id": iid})
        await db[SHARED_INTENTS].delete_many({"intent_id": iid})


# ── LIVE mode: TTL expiry transitions ─────────────────────────────
async def test_scan_expires_ttl_rows_when_live(monkeypatch):
    monkeypatch.setenv("PARADOX_V3_TRIGGER_WATCHER", "1")
    iid = f"t-ttl-{uuid.uuid4().hex[:10]}"
    await db[SHARED_INTENTS].insert_one({"intent_id": iid, "gate_state": "pending"})
    await enqueue_watch_plan(
        intent_id=iid, symbol="NVDA", lane="equity", stance="BULLISH",
        trigger_price=187.40, invalidation_price=184.20,
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=10),  # already expired
    )
    try:
        out = await scan_watch_queue()
        assert out["enabled"] is True
        assert out["expired"] >= 1
        # Queue row is now `expired`.
        row = await db[INTENT_WATCH_QUEUE_COLL].find_one({"intent_id": iid})
        assert row["state"] == "expired"
        # Intent's gate_state updated.
        intent = await db[SHARED_INTENTS].find_one({"intent_id": iid})
        assert intent["gate_state"] == "plan_expired"
    finally:
        await db[INTENT_WATCH_QUEUE_COLL].delete_many({"intent_id": iid})
        await db[SHARED_INTENTS].delete_many({"intent_id": iid})


async def test_scan_fires_bullish_trigger_when_live(monkeypatch):
    monkeypatch.setenv("PARADOX_V3_TRIGGER_WATCHER", "1")
    iid = f"t-fire-{uuid.uuid4().hex[:10]}"
    await db[SHARED_INTENTS].insert_one({"intent_id": iid, "gate_state": "pending"})
    await enqueue_watch_plan(
        intent_id=iid, symbol="NVDA", lane="equity", stance="BULLISH",
        trigger_price=100.0, invalidation_price=95.0,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=6),
    )

    async def fetcher(symbol, lane):
        return {"price": 101.5}

    try:
        out = await scan_watch_queue(price_fetcher=fetcher)
        assert out["fired"] == 1
        row = await db[INTENT_WATCH_QUEUE_COLL].find_one({"intent_id": iid})
        assert row["state"] == "fired"
        intent = await db[SHARED_INTENTS].find_one({"intent_id": iid})
        assert intent["gate_state"] == "trigger_fired"
    finally:
        await db[INTENT_WATCH_QUEUE_COLL].delete_many({"intent_id": iid})
        await db[SHARED_INTENTS].delete_many({"intent_id": iid})


async def test_scan_invalidates_bullish_when_price_drops(monkeypatch):
    monkeypatch.setenv("PARADOX_V3_TRIGGER_WATCHER", "1")
    iid = f"t-inv-{uuid.uuid4().hex[:10]}"
    await db[SHARED_INTENTS].insert_one({"intent_id": iid, "gate_state": "pending"})
    await enqueue_watch_plan(
        intent_id=iid, symbol="NVDA", lane="equity", stance="BULLISH",
        trigger_price=100.0, invalidation_price=95.0,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=6),
    )

    async def fetcher(symbol, lane):
        return {"price": 94.5}

    try:
        out = await scan_watch_queue(price_fetcher=fetcher)
        assert out["invalidated"] == 1
        row = await db[INTENT_WATCH_QUEUE_COLL].find_one({"intent_id": iid})
        assert row["state"] == "invalidated"
        intent = await db[SHARED_INTENTS].find_one({"intent_id": iid})
        assert intent["gate_state"] == "plan_invalidated"
    finally:
        await db[INTENT_WATCH_QUEUE_COLL].delete_many({"intent_id": iid})
        await db[SHARED_INTENTS].delete_many({"intent_id": iid})


async def test_scan_fires_bearish_trigger_inverted(monkeypatch):
    """BEARISH plans fire DOWN through trigger_price."""
    monkeypatch.setenv("PARADOX_V3_TRIGGER_WATCHER", "1")
    iid = f"t-bear-{uuid.uuid4().hex[:10]}"
    await db[SHARED_INTENTS].insert_one({"intent_id": iid, "gate_state": "pending"})
    await enqueue_watch_plan(
        intent_id=iid, symbol="NVDA", lane="equity", stance="BEARISH",
        trigger_price=100.0, invalidation_price=105.0,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=6),
    )

    async def fetcher(symbol, lane):
        return {"price": 98.0}

    try:
        out = await scan_watch_queue(price_fetcher=fetcher)
        assert out["fired"] == 1
        row = await db[INTENT_WATCH_QUEUE_COLL].find_one({"intent_id": iid})
        assert row["state"] == "fired"
    finally:
        await db[INTENT_WATCH_QUEUE_COLL].delete_many({"intent_id": iid})
        await db[SHARED_INTENTS].delete_many({"intent_id": iid})


async def test_scan_no_action_when_price_between_trigger_and_inv(monkeypatch):
    monkeypatch.setenv("PARADOX_V3_TRIGGER_WATCHER", "1")
    iid = f"t-mid-{uuid.uuid4().hex[:10]}"
    await db[SHARED_INTENTS].insert_one({"intent_id": iid, "gate_state": "pending"})
    await enqueue_watch_plan(
        intent_id=iid, symbol="NVDA", lane="equity", stance="BULLISH",
        trigger_price=100.0, invalidation_price=95.0,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=6),
    )

    async def fetcher(symbol, lane):
        return {"price": 97.5}

    try:
        out = await scan_watch_queue(price_fetcher=fetcher)
        assert out["fired"] == 0
        assert out["invalidated"] == 0
        # Row stays in `watching` state.
        row = await db[INTENT_WATCH_QUEUE_COLL].find_one({"intent_id": iid})
        assert row["state"] == "watching"
    finally:
        await db[INTENT_WATCH_QUEUE_COLL].delete_many({"intent_id": iid})
        await db[SHARED_INTENTS].delete_many({"intent_id": iid})


# ── Observability snapshot ────────────────────────────────────────
async def test_watch_queue_snapshot_returns_counts():
    snap = await watch_queue_snapshot(limit=10)
    assert "enabled" in snap
    assert "counts" in snap
    for state in ("watching", "fired", "invalidated", "expired"):
        assert state in snap["counts"]
    assert isinstance(snap["recent"], list)
