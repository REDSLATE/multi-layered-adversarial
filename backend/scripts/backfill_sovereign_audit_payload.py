"""One-shot — backfill `sovereign_audit_log` rows with the full
contribution content (2026-05-23).

The prior writer only stamped ts/brain/action/mode/training_signal/
delta_was_clamped/posted_as/seat_epoch. Every other field the brain
sent was discarded. The new writer carries them forward. This script
fills the gap for historical rows by joining each audit row to its
matching `sovereign_state_history` row (same brain + same ts) and
merging the missing fields IN-PLACE.

Idempotent: re-running is safe — we only set fields that aren't
already present.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from db import db  # noqa: E402


# Fields we want present on every audit row.
TARGET_FIELDS = (
    "live_trading_enabled", "weights", "learning_rate",
    "confidence_delta", "raw_confidence_delta", "delta_reason",
    "recent_outcomes", "notes",
)


async def main() -> None:
    print("Backfilling sovereign_audit_log rows from sovereign_state_history …")
    updated = 0
    skipped = 0
    no_match = 0
    has_substance_yes = 0
    has_substance_no = 0

    cursor = db.sovereign_audit_log.find(
        {"action": "contribution"}, {"_id": 1, "ts": 1, "brain": 1, **{f: 1 for f in TARGET_FIELDS}},
    )
    async for row in cursor:
        # If every target field already exists, skip.
        if all(f in row for f in TARGET_FIELDS):
            skipped += 1
            continue
        # Find matching history row (same brain + same ts).
        hist = await db.sovereign_state_history.find_one(
            {"brain": row.get("brain"), "ts": row.get("ts")},
            {"_id": 0, **{f: 1 for f in TARGET_FIELDS}},
        )
        if not hist:
            # Fall back to received_at match (older history rows may
            # only have received_at, not ts).
            hist = await db.sovereign_state_history.find_one(
                {"brain": row.get("brain"), "received_at": row.get("ts")},
                {"_id": 0, **{f: 1 for f in TARGET_FIELDS}},
            )
        if not hist:
            no_match += 1
            continue

        merge_fields = {}
        for f in TARGET_FIELDS:
            if f not in row and f in hist:
                merge_fields[f] = hist[f]

        # Derived: has_substance flag for the dashboard.
        notes = hist.get("notes") or ""
        weights = hist.get("weights") or {}
        outcomes = hist.get("recent_outcomes") or []
        delta_reason = hist.get("delta_reason") or ""
        confidence_delta = hist.get("confidence_delta") or 0.0
        has_substance = bool(
            (isinstance(notes, str) and notes.strip())
            or weights
            or outcomes
            or (isinstance(delta_reason, str) and delta_reason.strip())
            or confidence_delta != 0.0
        )
        merge_fields["has_substance"] = has_substance
        merge_fields["recent_outcomes_count"] = len(outcomes) if isinstance(outcomes, list) else 0

        if has_substance:
            has_substance_yes += 1
        else:
            has_substance_no += 1

        if merge_fields:
            await db.sovereign_audit_log.update_one(
                {"_id": row["_id"]},
                {"$set": merge_fields},
            )
            updated += 1

    print(f"  updated   : {updated}")
    print(f"  skipped   : {skipped}  (already full)")
    print(f"  no_match  : {no_match}  (no history row found)")
    print(f"  has_substance=True : {has_substance_yes}")
    print(f"  has_substance=False: {has_substance_no}")


if __name__ == "__main__":
    asyncio.run(main())
    sys.exit(0)
