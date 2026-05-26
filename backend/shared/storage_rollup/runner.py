"""Rollup runner — two phases:

  Phase 1 (rollup):  for each eligible row (older than
    ROLLUP_WINDOW_DAYS, not protected, not already rolled),
    derive movement+event labels and insert a slim row into
    `{collection}_rollups`. Stamp the original with `rolled_up_at`
    so re-runs are idempotent.

  Phase 2 (purge):   for each row whose `rolled_up_at` is older
    than ROLLUP_DELETE_HOLD_DAYS, delete the verbose original.
    The slim rollup row in `{collection}_rollups` is now canonical.

Both phases are idempotent. Phase 2 only ever touches rows Phase 1
has already rolled — never the verbose-original-without-rollup case.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from db import db
from shared.storage_rollup.config import (
    PROTECTED_COLLECTIONS,
    PROTECTED_FLAGS,
    PROTECTED_LABELS,
    ROLLUP_DELETE_HOLD_DAYS,
    ROLLUP_VERSION,
    ROLLUP_WINDOW_DAYS,
)
from shared.storage_rollup.derive import derive_event, derive_movement


logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _cutoff(days: int) -> datetime:
    return _now() - timedelta(days=days)


def is_protected(row: dict) -> bool:
    """Per-row protection check — never compress real-money executions
    or quarantine labels."""
    for k, v in PROTECTED_FLAGS.items():
        if row.get(k) == v:
            return True
    labels: set = set()
    for key in ("label", "labels", "memory_label"):
        value = row.get(key)
        if isinstance(value, list):
            labels.update(value)
        elif isinstance(value, str):
            labels.add(value)
    if labels & PROTECTED_LABELS:
        return True
    return False


def _build_rollup_doc(
    row: dict, collection_name: str, movement: str, event: str,
) -> dict:
    """Slim rollup row — keeps the analytical surface, drops payload.

    Sovereign rows get a couple extra keys (`mode`, `confidence_delta`,
    `delta_was_clamped`) because that's the analytical surface the
    operator cares about for sovereign-history rollups. None of those
    fields exist on intent / outcome / receipt rows, so they stay None
    there — kept lean."""
    doc = {
        "rollup_id": str(uuid4()),
        "rollup_version": ROLLUP_VERSION,
        "source_collection": collection_name,
        "source_id": row.get("_id"),
        "intent_id": row.get("intent_id"),
        "memory_id": row.get("memory_id"),
        "decision_id": row.get("decision_id"),
        "brain": row.get("brain") or row.get("stack") or row.get("runtime"),
        "symbol": row.get("symbol"),
        "lane": row.get("lane"),
        "action": row.get("action"),
        "movement": movement,
        "event": event,
        "confidence": row.get("confidence"),
        "rolled_at": _now(),
    }
    # Sovereign-specific analytical surface — only stamped when the
    # row IS a sovereign snapshot (signature check; matches the
    # derive.py heuristic).
    if (
        isinstance(row.get("mode"), str)
        and isinstance(row.get("learning_rate"), (int, float))
    ):
        doc["mode"] = row.get("mode")
        doc["confidence_delta"] = row.get("confidence_delta")
        doc["raw_confidence_delta"] = row.get("raw_confidence_delta")
        doc["delta_was_clamped"] = bool(row.get("delta_was_clamped"))
        doc["learning_rate"] = row.get("learning_rate")
        doc["posted_as"] = row.get("posted_as")
        doc["seat_epoch"] = row.get("seat_epoch")
    return doc


async def rollup_collection(
    collection_name: str,
    ts_field: str,
    dry_run: bool = True,
) -> dict:
    """Phase 1: derive labels for eligible rows, insert into
    `{name}_rollups`, mark original with `rolled_up_at`."""
    if collection_name in PROTECTED_COLLECTIONS:
        return {
            "collection": collection_name, "skipped": True,
            "reason": "protected_collection",
            "scanned": 0, "rolled": 0, "dry_run": dry_run,
        }

    # Existence check — brain-runtime collections won't be in MC's DB.
    existing = await db.list_collection_names(filter={"name": collection_name})
    if not existing:
        return {
            "collection": collection_name, "skipped": True,
            "reason": "collection_not_present_in_mc",
            "scanned": 0, "rolled": 0, "dry_run": dry_run,
        }

    cutoff_dt = _cutoff(ROLLUP_WINDOW_DAYS)
    # ts_field may be stored as ISO string OR BSON Date — query both
    # via Mongo $expr/$or so we don't miss either shape. ISO 8601 with
    # timezone sorts correctly lexicographically, so a string $lt
    # against cutoff_iso is chronologically correct.
    cutoff_iso = cutoff_dt.isoformat()
    query = {
        "$and": [
            {"rolled_up_at": {"$exists": False}},
            {"$or": [
                {ts_field: {"$lt": cutoff_dt}},
                {ts_field: {"$lt": cutoff_iso}},
            ]},
        ],
    }

    scanned = 0
    rolled = 0
    skipped_protected = 0
    skipped_ambiguous = 0

    cursor = db[collection_name].find(query)
    async for row in cursor:
        scanned += 1
        if is_protected(row):
            skipped_protected += 1
            continue
        movement = derive_movement(row)
        event = derive_event(row)
        if movement == "ambiguous" or event == "ambiguous":
            skipped_ambiguous += 1
            continue

        rollup_doc = _build_rollup_doc(row, collection_name, movement, event)
        if not dry_run:
            await db[f"{collection_name}_rollups"].insert_one(rollup_doc)
            await db[collection_name].update_one(
                {"_id": row["_id"]},
                {"$set": {
                    "rolled_up_at": _now(),
                    "rollup_id": rollup_doc["rollup_id"],
                    "rollup_version": ROLLUP_VERSION,
                }},
            )
        rolled += 1

    return {
        "collection": collection_name,
        "ts_field": ts_field,
        "dry_run": dry_run,
        "scanned": scanned,
        "rolled": rolled,
        "skipped_protected": skipped_protected,
        "skipped_ambiguous": skipped_ambiguous,
    }


async def purge_collection(
    collection_name: str, dry_run: bool = True,
) -> dict:
    """Phase 2: delete verbose originals whose `rolled_up_at` is older
    than ROLLUP_DELETE_HOLD_DAYS. Idempotent.

    Safety net: refuses to delete a row whose rollup doesn't exist in
    `{name}_rollups`. (Should never happen — Phase 1 writes the rollup
    before marking — but if it does, the original stays.)"""
    if collection_name in PROTECTED_COLLECTIONS:
        return {
            "collection": collection_name, "skipped": True,
            "reason": "protected_collection",
            "scanned": 0, "purged": 0, "dry_run": dry_run,
        }

    existing = await db.list_collection_names(filter={"name": collection_name})
    if not existing:
        return {
            "collection": collection_name, "skipped": True,
            "reason": "collection_not_present_in_mc",
            "scanned": 0, "purged": 0, "dry_run": dry_run,
        }

    hold_cutoff = _cutoff(ROLLUP_DELETE_HOLD_DAYS)
    query = {"rolled_up_at": {"$lt": hold_cutoff}}

    scanned = 0
    purged = 0
    safety_skipped = 0
    rollups_coll = f"{collection_name}_rollups"

    cursor = db[collection_name].find(
        query,
        {"_id": 1, "rollup_id": 1},
    )
    async for row in cursor:
        scanned += 1
        rid = row.get("rollup_id")
        # Safety: confirm the slim rollup row exists before deleting
        # the verbose original. If it doesn't, leave the original
        # alone and flag for operator review.
        if not rid:
            safety_skipped += 1
            continue
        found = await db[rollups_coll].find_one(
            {"rollup_id": rid}, {"_id": 1},
        )
        if not found:
            safety_skipped += 1
            continue
        if not dry_run:
            await db[collection_name].delete_one({"_id": row["_id"]})
        purged += 1

    return {
        "collection": collection_name,
        "dry_run": dry_run,
        "scanned": scanned,
        "purged": purged,
        "safety_skipped_missing_rollup": safety_skipped,
    }
