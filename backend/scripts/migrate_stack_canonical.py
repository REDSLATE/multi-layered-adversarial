"""One-time backfill: stamp `stack_canonical` on every historical
intent doc in `shared_intents`.

Doctrine (2026-02-23 dual-field migration):
  • `stack` is preserved verbatim — the field is never mutated.
  • `stack_canonical` is computed once from the legend and written
    to every doc missing it. Idempotent (re-running is a no-op).

Run inside the backend container:

    cd /app/backend && set -a && source .env && set +a && \
        python3 scripts/migrate_stack_canonical.py

The script prints a count summary and exits 0 on success. It is
safe to run during live operation — it filters on
`stack_canonical: {$exists: false}` so only un-migrated docs are
touched, and the update is a tight `update_many` per legacy code.
"""
from __future__ import annotations

import asyncio
import sys
from collections import Counter
from datetime import datetime, timezone

sys.path.insert(0, "/app/backend")

from db import db  # noqa: E402
from shared.brain_legend import (  # noqa: E402
    CANONICAL_BRAINS,
    LEGACY_TO_CANONICAL,
    seed_brain_legend,
)


SHARED_INTENTS = "shared_intents"


async def main() -> int:
    print("=" * 60)
    print("Stack canonical backfill — 2026-02-23 dual-field migration")
    print("=" * 60)

    # Step 1: ensure the legend collection is seeded.
    legend_summary = await seed_brain_legend(db)
    print(f"\n[1/4] brain_legend seeded: {legend_summary}")

    # Step 2: count pre-migration state.
    total = await db[SHARED_INTENTS].count_documents({})
    already = await db[SHARED_INTENTS].count_documents(
        {"stack_canonical": {"$exists": True}},
    )
    to_migrate = total - already
    print(
        f"\n[2/4] Pre-migration: total={total:,} "
        f"already_migrated={already:,} to_migrate={to_migrate:,}",
    )
    if to_migrate == 0:
        print("\nNothing to migrate — exiting cleanly.")
        return 0

    # Step 3: per-stack-code update_many.
    print("\n[3/4] Running per-code update_many ...")
    started = datetime.now(timezone.utc)
    per_code: Counter[str] = Counter()

    # Canonical-stamp-self for canonical docs missing the field.
    for canonical in sorted(CANONICAL_BRAINS):
        result = await db[SHARED_INTENTS].update_many(
            {"stack": canonical, "stack_canonical": {"$exists": False}},
            {"$set": {"stack_canonical": canonical}},
        )
        per_code[canonical] = result.modified_count
        print(
            f"   {canonical!r:>14s} → {canonical!r:>14s} : "
            f"{result.modified_count:>7,} docs updated",
        )

    # Legacy → canonical.
    for legacy, canonical in sorted(LEGACY_TO_CANONICAL.items()):
        result = await db[SHARED_INTENTS].update_many(
            {"stack": legacy, "stack_canonical": {"$exists": False}},
            {"$set": {"stack_canonical": canonical}},
        )
        per_code[legacy] = result.modified_count
        print(
            f"   {legacy!r:>14s} → {canonical!r:>14s} : "
            f"{result.modified_count:>7,} docs updated",
        )

    # Anything else (unknown stack values) gets the field stamped
    # with the lowercased raw value so downstream code can group
    # by `stack_canonical` without losing rows.
    cursor = db[SHARED_INTENTS].find(
        {"stack_canonical": {"$exists": False}},
        {"_id": 1, "stack": 1},
    )
    unknown_count = 0
    async for doc in cursor:
        raw = (doc.get("stack") or "").strip().lower()
        if not raw:
            raw = "__missing__"
        await db[SHARED_INTENTS].update_one(
            {"_id": doc["_id"]},
            {"$set": {"stack_canonical": raw}},
        )
        unknown_count += 1
    if unknown_count:
        print(
            f"   {'(unknown)':>14s} → {'<lowercased raw>':>14s} : "
            f"{unknown_count:>7,} docs updated (review these — "
            f"unexpected `stack` values in your data)",
        )

    elapsed = (datetime.now(timezone.utc) - started).total_seconds()

    # Step 4: post-migration verify.
    still_missing = await db[SHARED_INTENTS].count_documents(
        {"stack_canonical": {"$exists": False}},
    )
    print(
        f"\n[4/4] Post-migration verify: still_missing={still_missing} "
        f"(must be 0)",
    )
    if still_missing != 0:
        print("\nERROR — migration left rows un-stamped. Investigate.")
        return 2

    # Index on stack_canonical for fast aggregation reads.
    await db[SHARED_INTENTS].create_index(
        "stack_canonical",
        name="ix_shared_intents_stack_canonical",
        background=True,
    )
    print("\nIndex ix_shared_intents_stack_canonical created (background).")

    # Print stack_canonical distribution for sanity check.
    print("\n=== Post-migration stack_canonical distribution ===")
    pipeline = [
        {"$group": {"_id": "$stack_canonical", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
    ]
    async for d in db[SHARED_INTENTS].aggregate(pipeline):
        print(f"   {d['_id']!r:>14s} : {d['count']:>7,}")

    print(f"\nDone. Elapsed: {elapsed:.1f}s. Total updated: {sum(per_code.values()) + unknown_count:,}.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
