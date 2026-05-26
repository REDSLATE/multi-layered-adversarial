"""Tripwires — Storage Rollup doctrine (2026-05-26).

Doctrine guards:
  1. Protected collections (Shellys, brain_memories, quarantine
     labels) are NEVER rolled up.
  2. Executed real-money trades are NEVER rolled up.
  3. Movement derivation is faithful — `BUY/OPEN→long`, `SHORT→short`,
     etc. — never guessed.
  4. Rollup is idempotent — re-running picks no new rows.
  5. Phase 2 (purge) refuses to delete a row whose slim rollup
     doesn't exist (safety net).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from db import db
from shared.storage_rollup.config import (
    PROTECTED_COLLECTIONS,
    ROLLUP_DELETE_HOLD_DAYS,
    ROLLUP_WINDOW_DAYS,
)
from shared.storage_rollup.derive import derive_event, derive_movement
from shared.storage_rollup.runner import (
    is_protected,
    purge_collection,
    rollup_collection,
)


pytestmark = pytest.mark.asyncio


# ─────────── unit-level derivation ───────────


def test_buy_derives_long():
    assert derive_movement({"action": "BUY"}) == "long"


def test_open_derives_long():
    assert derive_movement({"action": "OPEN"}) == "long"


def test_short_derives_short():
    assert derive_movement({"action": "SHORT"}) == "short"


def test_sell_derives_flat():
    assert derive_movement({"action": "SELL"}) == "flat"


def test_hold_derives_flat():
    assert derive_movement({"action": "HOLD"}) == "flat"


def test_blocked_gate_derives_blocked():
    assert derive_movement({"action": "BUY", "gate_state": "blocked"}) == "blocked"


def test_rejected_at_ingest_derives_rejected():
    row = {"action": "BUY", "gate_state": "rejected_at_ingest"}
    assert derive_movement(row) == "rejected"


def test_blocked_event_carries_gate_name():
    row = {"action": "BUY", "blocked_by": "roadguard_spread_floor"}
    assert derive_event(row) == "blocked_roadguard_spread_floor"


def test_executed_win_event():
    row = {"executed": True, "outcome": "win"}
    assert derive_event(row) == "executed_win"


def test_executed_loss_event():
    row = {"executed": True, "outcome": "loss"}
    assert derive_event(row) == "executed_loss"


def test_executed_scratch_event():
    row = {"executed": True, "outcome": "scratch"}
    assert derive_event(row) == "executed_scratch"


def test_shadow_observation_default():
    row = {"action": "BUY", "executed": False}
    assert derive_event(row) == "shadow_observation"


# ─────────── protection guards ───────────


def test_executed_row_is_protected():
    assert is_protected({"executed": True}) is True


def test_live_order_row_is_protected():
    assert is_protected({"live_order": True}) is True


def test_real_money_row_is_protected():
    assert is_protected({"real_money": True}) is True


def test_quarantine_label_is_protected():
    assert is_protected({"label": "quarantine"}) is True


def test_quarantine_in_labels_list_is_protected():
    assert is_protected({"labels": ["safe", "quarantine"]}) is True


def test_non_protected_row():
    assert is_protected({"action": "BUY", "executed": False}) is False


# ─────────── collection-level guards ───────────


def test_mc_shelly_in_protected_collections():
    assert "mc_shelly" in PROTECTED_COLLECTIONS


def test_brain_memories_in_protected_collections():
    assert "brain_memories" in PROTECTED_COLLECTIONS


def test_shared_labeled_memories_in_protected_collections():
    assert "shared_labeled_memories" in PROTECTED_COLLECTIONS


def test_per_brain_shellys_in_protected_collections():
    for b in ("alpha", "camaro", "chevelle", "redeye"):
        assert f"{b}_shelly" in PROTECTED_COLLECTIONS
        assert f"{b}_brain_memories" in PROTECTED_COLLECTIONS


# ─────────── runner integration ───────────


async def _seed_old_intent(action: str, executed: bool = False, **extra):
    """Insert an intent dated 90 days ago into shared_intents and
    return its _id. Marker so we can clean up."""
    ts = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
    doc = {
        "intent_id": f"_rollup_test_{action}_{executed}_{ts}",
        "stack": "camaro",
        "action": action,
        "symbol": "AAPL",
        "lane": "equity",
        "gate_state": "passed" if executed else "rejected_at_ingest",
        "executed": executed,
        "ingest_ts": ts,
        "confidence": 0.5,
        "_rollup_test_": True,
    }
    doc.update(extra)
    await db["shared_intents"].insert_one(doc)
    return doc["intent_id"]


async def _cleanup():
    await db["shared_intents"].delete_many({"_rollup_test_": True})
    await db["shared_intents_rollups"].delete_many({"intent_id": {"$regex": "^_rollup_test_"}})


async def test_old_rejected_row_gets_rolled_up():
    await _cleanup()
    iid = await _seed_old_intent("BUY", executed=False)
    res = await rollup_collection("shared_intents", "ingest_ts", dry_run=False)
    assert res["rolled"] >= 1
    original = await db["shared_intents"].find_one({"intent_id": iid})
    assert original is not None
    assert original.get("rolled_up_at") is not None
    rollup = await db["shared_intents_rollups"].find_one({"intent_id": iid})
    assert rollup is not None
    assert rollup["movement"] == "rejected"
    # The slim rollup MUST omit verbose fields. Even if `confidence`
    # is kept (analytical surface), `rationale`/`evidence` must not.
    assert "rationale" not in rollup
    assert "evidence" not in rollup
    assert "doctrine_packet" not in rollup
    await _cleanup()


async def test_executed_row_never_rolled():
    await _cleanup()
    iid = await _seed_old_intent("BUY", executed=True, gate_state="passed",
                                 outcome="win")
    await rollup_collection("shared_intents", "ingest_ts", dry_run=False)
    original = await db["shared_intents"].find_one({"intent_id": iid})
    assert original is not None
    assert original.get("rolled_up_at") is None
    rollup = await db["shared_intents_rollups"].find_one({"intent_id": iid})
    assert rollup is None
    await _cleanup()


async def test_protected_collection_skipped():
    """Calling rollup against mc_shelly MUST return skipped reason
    without doing any work — even if rows match the cutoff."""
    res = await rollup_collection("mc_shelly", "ts", dry_run=False)
    assert res["skipped"] is True
    assert res["reason"] == "protected_collection"
    assert res["rolled"] == 0


async def test_rollup_is_idempotent():
    """Two runs in a row pick zero new rows on the second pass."""
    await _cleanup()
    await _seed_old_intent("BUY", executed=False)
    r1 = await rollup_collection("shared_intents", "ingest_ts", dry_run=False)
    r2 = await rollup_collection("shared_intents", "ingest_ts", dry_run=False)
    assert r1["rolled"] >= 1
    assert r2["rolled"] == 0   # second pass — nothing left to do
    await _cleanup()


async def test_recent_row_not_rolled():
    """A row inside the window (recent) MUST be untouched."""
    await _cleanup()
    ts_now = datetime.now(timezone.utc).isoformat()
    iid = f"_rollup_test_recent_{ts_now}"
    await db["shared_intents"].insert_one({
        "intent_id": iid, "stack": "alpha", "action": "BUY",
        "symbol": "AAPL", "lane": "equity",
        "gate_state": "passed", "executed": False,
        "ingest_ts": ts_now, "_rollup_test_": True,
    })
    await rollup_collection("shared_intents", "ingest_ts", dry_run=False)
    row = await db["shared_intents"].find_one({"intent_id": iid})
    assert row.get("rolled_up_at") is None
    await _cleanup()


# ─────────── Phase 2 / purge safety ───────────


async def test_purge_protects_collection():
    res = await purge_collection("mc_shelly", dry_run=False)
    assert res["skipped"] is True
    assert res["reason"] == "protected_collection"


async def test_purge_refuses_when_rollup_missing():
    """A row marked `rolled_up_at` (old hold expired) whose slim
    rollup doc is missing MUST NOT be deleted — that's the safety
    net for a corrupted rollup state."""
    await _cleanup()
    # Insert an intent that LOOKS rolled up but has no rollup row.
    rolled_at = datetime.now(timezone.utc) - timedelta(
        days=ROLLUP_DELETE_HOLD_DAYS + 1,
    )
    iid = f"_rollup_test_orphan_{rolled_at.isoformat()}"
    await db["shared_intents"].insert_one({
        "intent_id": iid, "stack": "camaro", "action": "BUY",
        "symbol": "AAPL", "lane": "equity",
        "executed": False, "rolled_up_at": rolled_at,
        "rollup_id": "nonexistent-rollup-id",
        "_rollup_test_": True,
    })
    res = await purge_collection("shared_intents", dry_run=False)
    assert res["safety_skipped_missing_rollup"] >= 1
    still_there = await db["shared_intents"].find_one({"intent_id": iid})
    assert still_there is not None
    await _cleanup()


async def test_purge_deletes_after_hold_expires():
    """After the 7-day hold, a properly-rolled row gets purged."""
    await _cleanup()
    rolled_at = datetime.now(timezone.utc) - timedelta(
        days=ROLLUP_DELETE_HOLD_DAYS + 1,
    )
    iid = f"_rollup_test_purgeable_{rolled_at.isoformat()}"
    rollup_id = "purge-rollup-1"
    # Verbose original
    await db["shared_intents"].insert_one({
        "intent_id": iid, "stack": "camaro", "action": "BUY",
        "symbol": "AAPL", "lane": "equity",
        "executed": False, "rolled_up_at": rolled_at,
        "rollup_id": rollup_id,
        "_rollup_test_": True,
    })
    # Slim rollup row
    await db["shared_intents_rollups"].insert_one({
        "rollup_id": rollup_id, "intent_id": iid,
        "movement": "rejected", "event": "rejected_at_ingest",
    })
    res = await purge_collection("shared_intents", dry_run=False)
    assert res["purged"] >= 1
    deleted = await db["shared_intents"].find_one({"intent_id": iid})
    assert deleted is None
    # Slim row survives — that's the analytical surface we keep.
    surviving_rollup = await db["shared_intents_rollups"].find_one(
        {"rollup_id": rollup_id},
    )
    assert surviving_rollup is not None
    await db["shared_intents_rollups"].delete_one({"rollup_id": rollup_id})
    await _cleanup()


async def test_dry_run_writes_nothing():
    await _cleanup()
    iid = await _seed_old_intent("BUY", executed=False)
    res = await rollup_collection("shared_intents", "ingest_ts", dry_run=True)
    assert res["rolled"] >= 1
    original = await db["shared_intents"].find_one({"intent_id": iid})
    assert original.get("rolled_up_at") is None
    rollup = await db["shared_intents_rollups"].find_one({"intent_id": iid})
    assert rollup is None
    await _cleanup()
