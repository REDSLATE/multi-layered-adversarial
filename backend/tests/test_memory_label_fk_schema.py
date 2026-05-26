"""Tripwires — `shared_labeled_memories.memory_id` FK doctrine (2026-05-25).

Cover the four invariants of the schema-tightening:

  1. /api/ingest/memory-labels now accepts `memory_id` + `decision_id`.
  2. They're persisted on the row (not just bounced).
  3. The cross-brain endpoint's `_quarantined_memory_ids` uses the FK
     when present and falls back to regex for legacy rows.
  4. The backfill script is idempotent and only mutates rows missing
     the FK.

These tests are doctrine tripwires — they must pass. They explicitly
do NOT test brain-side modulator math (that's the brain's house);
they test only MC's contract.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from db import db
from namespaces import SHARED_MEMORY


pytestmark = pytest.mark.asyncio


# ─────────────────── unit-level model tripwires ───────────────────


def test_memory_label_in_accepts_memory_id():
    """`MemoryLabelIn` MUST accept the new FK fields."""
    from shared.ingest import MemoryLabelIn
    m = MemoryLabelIn(
        runtime="alpha", label="quarantine",
        reason="poisoned",
        memory_id="mem-abc-123",
        decision_id="dec-xyz-789",
    )
    assert m.memory_id == "mem-abc-123"
    assert m.decision_id == "dec-xyz-789"


def test_memory_label_in_remains_back_compat():
    """Legacy emitters (no FK fields) MUST still validate."""
    from shared.ingest import MemoryLabelIn
    m = MemoryLabelIn(runtime="camaro", label="safe", payload_summary="ok")
    assert m.memory_id is None
    assert m.decision_id is None


# ─────────────────── DB-roundtrip tripwires ───────────────────


async def _clear_memory_collection():
    await db[SHARED_MEMORY].delete_many({"_test_": True})


async def test_fk_persisted_on_insert():
    """Direct DB write through the same shape the route uses MUST
    surface the FK on read."""
    await _clear_memory_collection()
    doc = {
        "id": "test-1",
        "runtime": "alpha",
        "label": "quarantine",
        "reason": "test poison",
        "payload_summary": "",
        "memory_id": "mem-fk-1",
        "decision_id": "dec-fk-1",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "_test_": True,
    }
    await db[SHARED_MEMORY].insert_one(doc)
    found = await db[SHARED_MEMORY].find_one(
        {"id": "test-1"}, {"_id": 0, "memory_id": 1, "decision_id": 1},
    )
    assert found == {"memory_id": "mem-fk-1", "decision_id": "dec-fk-1"}
    await _clear_memory_collection()


async def test_quarantine_set_uses_fk_directly():
    """The cross-brain endpoint MUST pick up FK-bearing rows without
    needing the regex fallback."""
    from routes.runtime_cross_brain_memories import _quarantined_memory_ids
    await _clear_memory_collection()
    await db[SHARED_MEMORY].insert_one({
        "id": "test-q-1",
        "runtime": "alpha",
        "label": "quarantine",
        "memory_id": "mem-q-fk",
        "decision_id": None,
        "payload_summary": "",      # intentionally empty — proves FK is used
        "reason": "",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "_test_": True,
    })
    ids = await _quarantined_memory_ids("AAPL")
    assert "mem-q-fk" in ids
    await _clear_memory_collection()


async def test_quarantine_set_falls_back_to_regex():
    """Legacy rows (no FK, decision_id only in payload_summary) MUST
    still be picked up so historical quarantines stay enforced."""
    from routes.runtime_cross_brain_memories import _quarantined_memory_ids
    await _clear_memory_collection()
    await db[SHARED_MEMORY].insert_one({
        "id": "test-legacy",
        "runtime": "alpha",
        "label": "quarantine",
        "memory_id": None,
        "decision_id": None,
        "payload_summary": "legacy row decision_id=legacy-dec-77 details",
        "reason": "",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "_test_": True,
    })
    ids = await _quarantined_memory_ids("AAPL")
    assert "legacy-dec-77" in ids
    await _clear_memory_collection()


async def test_fk_and_legacy_rows_union():
    """Mixed corpus: BOTH FK-stamped and legacy rows MUST contribute
    to the quarantine set."""
    from routes.runtime_cross_brain_memories import _quarantined_memory_ids
    await _clear_memory_collection()
    await db[SHARED_MEMORY].insert_many([
        {
            "id": "test-mix-fk",
            "runtime": "alpha", "label": "quarantine",
            "memory_id": "mem-mix-1", "decision_id": None,
            "payload_summary": "", "reason": "",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "_test_": True,
        },
        {
            "id": "test-mix-legacy",
            "runtime": "camaro", "label": "quarantine",
            "memory_id": None, "decision_id": None,
            "payload_summary": "decision_id=mix-legacy-2",
            "reason": "",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "_test_": True,
        },
    ])
    ids = await _quarantined_memory_ids("AAPL")
    assert "mem-mix-1" in ids
    assert "mix-legacy-2" in ids
    await _clear_memory_collection()


# ─────────────────── backfill tripwires ───────────────────


async def test_backfill_idempotent_on_already_fk_rows():
    """Backfill MUST NOT touch rows that already carry the FK."""
    from scripts.backfill_memory_label_fk import backfill
    await _clear_memory_collection()
    await db[SHARED_MEMORY].insert_one({
        "id": "test-bf-1",
        "runtime": "alpha", "label": "quarantine",
        "memory_id": "already-set",
        "decision_id": None,
        "payload_summary": "decision_id=should-be-ignored",
        "reason": "",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "_test_": True,
    })
    result = await backfill(dry_run=False)
    assert result["already_done"] >= 1
    # The row's memory_id must not have been overwritten by the regex.
    row = await db[SHARED_MEMORY].find_one(
        {"id": "test-bf-1"}, {"_id": 0, "memory_id": 1},
    )
    assert row["memory_id"] == "already-set"
    await _clear_memory_collection()


async def test_backfill_writes_fk_from_legacy_payload():
    """A legacy row missing the FK MUST get its memory_id /
    decision_id stamped by the script."""
    from scripts.backfill_memory_label_fk import backfill
    await _clear_memory_collection()
    await db[SHARED_MEMORY].insert_one({
        "id": "test-bf-legacy",
        "runtime": "alpha", "label": "quarantine",
        "memory_id": None,
        "decision_id": None,
        "payload_summary": "memory_id=bf-mem decision_id=bf-dec",
        "reason": "",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "_test_": True,
    })
    result = await backfill(dry_run=False)
    assert result["backfilled"] >= 1
    row = await db[SHARED_MEMORY].find_one(
        {"id": "test-bf-legacy"},
        {"_id": 0, "memory_id": 1, "decision_id": 1},
    )
    assert row["memory_id"] == "bf-mem"
    assert row["decision_id"] == "bf-dec"
    await _clear_memory_collection()


async def test_backfill_dry_run_writes_nothing():
    """Dry-run mode MUST NOT mutate the collection."""
    from scripts.backfill_memory_label_fk import backfill
    await _clear_memory_collection()
    await db[SHARED_MEMORY].insert_one({
        "id": "test-bf-dry",
        "runtime": "alpha", "label": "quarantine",
        "memory_id": None,
        "decision_id": None,
        "payload_summary": "decision_id=dry-dec",
        "reason": "",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "_test_": True,
    })
    result = await backfill(dry_run=True)
    assert result["dry_run"] is True
    row = await db[SHARED_MEMORY].find_one(
        {"id": "test-bf-dry"}, {"_id": 0, "memory_id": 1, "decision_id": 1},
    )
    # Dry run leaves the FKs untouched.
    assert row.get("memory_id") is None
    assert row.get("decision_id") is None
    await _clear_memory_collection()
