"""Seat-state single-source-of-truth tests (2026-02-20).

Covers:
  * `mirror_executor_to_v2_trust` — single-holder semantics + clear.
  * `migrate_legacy_auditor_to_roster` — idempotent boot migration.
  * `cleanup_legacy_collections` — drops two legacy collections.
  * `executor_seat.get_seat_holder` — no longer falls back to legacy
    doc; roster is the only read source.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest

if not os.environ.get("MONGO_URL"):  # pragma: no cover
    pytest.skip("MONGO_URL not set; seat-state tests need a live Mongo", allow_module_level=True)


from db import db
from namespaces import (
    BRAIN_ROSTER,
    PARADOX_V2_SEAT_TRUSTED,
    SHARED_AUDITOR_SEAT,
    SHARED_EXECUTOR_SEAT,
)
from shared.seat_state import (
    cleanup_legacy_collections,
    migrate_legacy_auditor_to_roster,
    mirror_executor_to_v2_trust,
)


_TEST_AUDITOR_BRAIN = "test_auditor_brain_xyz"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _snapshot(coll_name: str) -> list[dict]:
    return [doc async for doc in db[coll_name].find({})]


async def _restore(coll_name: str, docs: list[dict]) -> None:
    await db[coll_name].drop()
    if docs:
        await db[coll_name].insert_many(docs)


# ── mirror_executor_to_v2_trust ───────────────────────────────────────


@pytest.mark.asyncio
async def test_mirror_executor_to_v2_trust_single_holder_semantics():
    snap = await _snapshot(PARADOX_V2_SEAT_TRUSTED)
    try:
        # Assign.
        await mirror_executor_to_v2_trust("executor", "alpha")
        rows = [r async for r in db[PARADOX_V2_SEAT_TRUSTED].find(
            {"seat_id": "equity_executor"},
        )]
        assert len(rows) == 1
        assert rows[0]["brain_id"] == "alpha"

        # Reassign — replace, not append.
        await mirror_executor_to_v2_trust("executor", "camaro")
        rows = [r async for r in db[PARADOX_V2_SEAT_TRUSTED].find(
            {"seat_id": "equity_executor"},
        )]
        assert len(rows) == 1
        assert rows[0]["brain_id"] == "camaro"

        # Clear.
        await mirror_executor_to_v2_trust("executor", None)
        rows = [r async for r in db[PARADOX_V2_SEAT_TRUSTED].find(
            {"seat_id": "equity_executor"},
        )]
        assert len(rows) == 0

        # Non-executor role is a no-op.
        await mirror_executor_to_v2_trust("strategist", "alpha")
        rows = [r async for r in db[PARADOX_V2_SEAT_TRUSTED].find(
            {"seat_id": "equity_strategist"},
        )]
        assert len(rows) == 0
    finally:
        await _restore(PARADOX_V2_SEAT_TRUSTED, snap)


# ── migrate_legacy_auditor_to_roster ──────────────────────────────────


@pytest.mark.asyncio
async def test_migrate_legacy_auditor_to_roster_idempotent():
    snap_audit = await _snapshot(SHARED_AUDITOR_SEAT)
    snap_roster = await _snapshot(BRAIN_ROSTER)
    try:
        # Legacy has holder; roster auditor is None.
        await db[SHARED_AUDITOR_SEAT].replace_one(
            {"_id": "auditor"},
            {"_id": "auditor", "holder": _TEST_AUDITOR_BRAIN, "since": _now()},
            upsert=True,
        )
        await db[BRAIN_ROSTER].update_one(
            {"_id": "current"},
            {"$set": {"assignments.auditor": None}},
            upsert=True,
        )

        r1 = await migrate_legacy_auditor_to_roster()
        assert r1["migrated"] is True
        assert r1["auditor_holder"] == _TEST_AUDITOR_BRAIN

        doc = await db[BRAIN_ROSTER].find_one({"_id": "current"}, {"_id": 0})
        assert doc["assignments"]["auditor"] == _TEST_AUDITOR_BRAIN

        # Idempotent: re-run is a no-op.
        r2 = await migrate_legacy_auditor_to_roster()
        assert r2["migrated"] is False
        assert r2["reason"] == "roster_already_has_auditor"

        # Edge: legacy missing.
        await db[SHARED_AUDITOR_SEAT].delete_many({})
        r3 = await migrate_legacy_auditor_to_roster()
        assert r3["migrated"] is False
        assert r3["reason"] == "no_legacy_auditor_holder"
    finally:
        await _restore(SHARED_AUDITOR_SEAT, snap_audit)
        await _restore(BRAIN_ROSTER, snap_roster)


# ── cleanup_legacy_collections ────────────────────────────────────────


@pytest.mark.asyncio
async def test_cleanup_legacy_collections_drops_both():
    snap_exec = await _snapshot(SHARED_EXECUTOR_SEAT)
    snap_audit = await _snapshot(SHARED_AUDITOR_SEAT)
    try:
        await db[SHARED_EXECUTOR_SEAT].insert_one(
            {"_id": "executor_test_xyz", "holder": "test"},
        )
        await db[SHARED_AUDITOR_SEAT].insert_one(
            {"_id": "auditor_test_xyz", "holder": "test"},
        )
        result = await cleanup_legacy_collections()
        assert set(result["dropped"]) == {
            "shared_executor_seat", "shared_auditor_seat",
        }
        assert result["rows_removed"]["shared_executor_seat"] >= 1
        assert result["rows_removed"]["shared_auditor_seat"] >= 1
        assert await db[SHARED_EXECUTOR_SEAT].count_documents({}) == 0
        assert await db[SHARED_AUDITOR_SEAT].count_documents({}) == 0
    finally:
        await _restore(SHARED_EXECUTOR_SEAT, snap_exec)
        await _restore(SHARED_AUDITOR_SEAT, snap_audit)


# ── executor_seat.get_seat_holder no-fallback contract ────────────────


@pytest.mark.asyncio
async def test_get_seat_holder_does_not_fall_back_to_legacy_doc():
    """Doctrine pin: `get_seat_holder('executor')` must ONLY return the
    roster's executor assignment. Even if the legacy
    `shared_executor_seat` doc has a holder, it MUST NOT override.
    """
    from shared.executor_seat import get_seat_holder
    snap_exec = await _snapshot(SHARED_EXECUTOR_SEAT)
    snap_roster = await _snapshot(BRAIN_ROSTER)
    try:
        await db[BRAIN_ROSTER].update_one(
            {"_id": "current"},
            {"$set": {"assignments.executor": None}},
            upsert=True,
        )
        await db[SHARED_EXECUTOR_SEAT].replace_one(
            {"_id": "executor"},
            {"_id": "executor", "holder": "alpha", "since": _now()},
            upsert=True,
        )
        holder = await get_seat_holder("executor")
        assert holder is None, (
            f"expected None (roster says vacant) but got {holder!r} "
            "— legacy fallback still active?"
        )
    finally:
        await _restore(SHARED_EXECUTOR_SEAT, snap_exec)
        await _restore(BRAIN_ROSTER, snap_roster)
