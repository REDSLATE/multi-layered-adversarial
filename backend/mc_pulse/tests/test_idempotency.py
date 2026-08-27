"""Idempotency: a retried pulse must never double-execute."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal

import pytest
import pytest_asyncio

from db import db
from mc_arbiter.models import Direction, ModelOpinion
from mc_pulse.envelope import OpinionEnvelope
from mc_pulse.pulse import _upsert_envelopes
from mc_pulse.snapshot import build_snapshot


def _envelope(pulse_id: str, brain: str, symbol: str = "TESTIDEM"):
    snap = build_snapshot(
        symbol=symbol, lane="equity",
        timestamp=datetime(2026, 7, 11, 14, 30, tzinfo=timezone.utc),
        price=Decimal("100.00"),
        indicators={},
    )
    op = ModelOpinion(
        brain=brain,
        seat_key=f"equity:{symbol}:2026-07-11T14:30:00Z",
        direction=Direction.LONG,
        edge=0.5, confidence=0.6, regime_fit=0.7, urgency=0.5,
        price_at_signal=100.0,
        ts=snap.timestamp.isoformat(),
    )
    return OpinionEnvelope(
        pulse_id=pulse_id,
        brain_id=brain,
        seat_key=op.seat_key,
        snapshot_id=snap.snapshot_id,
        opinion=op,
        evaluated_at=datetime.now(timezone.utc),
    )


@pytest_asyncio.fixture
async def compare_collection_cleanup():
    """Wipe the migration comparison collection between tests.

    2026-02-11: switched to `@pytest_asyncio.fixture` + native `async`
    teardown. The old `asyncio.get_event_loop().run_until_complete(...)`
    call fired AFTER pytest-asyncio had closed the test's event loop,
    which crashed `motor` with "There is no current event loop in
    thread" — but only when this file ran late in the suite (order-
    dependent). Native async teardown shares the same loop as the
    test that owned the fixture, so motor stays happy.
    """
    yield
    await db["mc_opinions_compare"].delete_many(
        {"symbol": {"$regex": "^TESTIDEM"}},
    )


@pytest.mark.asyncio
async def test_upsert_is_idempotent_on_retry(compare_collection_cleanup):
    """First call inserts; second call with the SAME pulse_id
    updates in place. NO duplicate row."""
    env = _envelope("p_retry_1", "camino", symbol="TESTIDEMA")
    await _upsert_envelopes([env], "mc_opinions_compare")
    await _upsert_envelopes([env], "mc_opinions_compare")
    rows = await db["mc_opinions_compare"].find(
        {"pulse_id": "p_retry_1"},
    ).to_list(10)
    assert len(rows) == 1
    assert rows[0]["brain"] == "camino"


@pytest.mark.asyncio
async def test_same_bucket_pulses_collapse_to_one_row(compare_collection_cleanup):
    """2026-07-14 iter-30 doctrine: upsert key is `(seat_key, brain)`.
    Two pulses landing in the SAME 5-min seat bucket for the same
    brain/symbol/lane must UPDATE one row (latest pulse wins), never
    insert a duplicate — the old pulse_id-scoped filter caused the
    E11000 flood. (This test previously pinned the old behavior.)"""
    env_a = _envelope("p_diff_A", "camino", symbol="TESTIDEMB")
    env_b = _envelope("p_diff_B", "camino", symbol="TESTIDEMB")
    await _upsert_envelopes([env_a], "mc_opinions_compare")
    await _upsert_envelopes([env_b], "mc_opinions_compare")
    rows = await db["mc_opinions_compare"].find(
        {"symbol": "TESTIDEMB"},
    ).to_list(10)
    assert len(rows) == 1
    assert rows[0]["pulse_id"] == "p_diff_B"


@pytest.mark.asyncio
async def test_different_brains_same_pulse_are_separate_rows(compare_collection_cleanup):
    """Same pulse, different brains → separate rows (the composite
    key includes brain)."""
    env_c = _envelope("p_multi", "camino", symbol="TESTIDEMC")
    env_h = _envelope("p_multi", "hellcat", symbol="TESTIDEMC")
    await _upsert_envelopes([env_c, env_h], "mc_opinions_compare")
    rows = await db["mc_opinions_compare"].find(
        {"pulse_id": "p_multi"},
    ).to_list(10)
    assert len(rows) == 2
    assert {r["brain"] for r in rows} == {"camino", "hellcat"}


@pytest.mark.asyncio
async def test_first_recorded_at_stays_stable_across_retries(compare_collection_cleanup):
    """`$setOnInsert.first_recorded_at` must remain stable — a
    retry cannot rewrite the insertion timestamp. Preserves the
    audit trail's honesty about when the row FIRST landed vs when
    it was last updated."""
    env = _envelope("p_first", "gto", symbol="TESTIDEMD")
    await _upsert_envelopes([env], "mc_opinions_compare")
    row1 = await db["mc_opinions_compare"].find_one({"pulse_id": "p_first"})
    original_first = row1["first_recorded_at"]
    # Retry — should update, keep first_recorded_at.
    await asyncio.sleep(0.01)
    await _upsert_envelopes([env], "mc_opinions_compare")
    row2 = await db["mc_opinions_compare"].find_one({"pulse_id": "p_first"})
    assert row2["first_recorded_at"] == original_first
