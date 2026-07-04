"""One-shot migration: canonicalize `brain` field on legacy observation_receipts rows.

Doctrine pin (P2, 2026-07-04): the observation_receipts collection contains
8,553 rows filed under the legacy stack names (alpha/camaro/chevelle/redeye)
that predated the 2026-06-09 rename to canonical brain_ids
(camino/barracuda/hellcat/gto). The list endpoint validates `brain in
CANONICAL_BRAINS` so those rows are unreachable via
`GET /api/admin/observation-receipts?brain=camaro&lane=equity` — they
return 400. Migrating in place fixes discoverability without changing any
downstream logic.

Usage:
    # Dry-run (default): counts how many rows would change, no writes
    python backend/scripts/migrate_observation_receipts_legacy_brain_names.py

    # Apply the migration
    python backend/scripts/migrate_observation_receipts_legacy_brain_names.py --apply

Idempotent: rerunning after successful apply is a no-op (no rows match
the legacy-name filter).

Non-destructive: the original brain name is preserved on the doc as
`brain_original_legacy` so the migration is traceable and manually
reversible if needed (see rollback plan in the sign-off doc).
"""
from __future__ import annotations

import argparse
import asyncio
import os

from motor.motor_asyncio import AsyncIOMotorClient


# Frozen mapping — mirrors `LEGACY_TO_CANONICAL` in
# backend/shared/brain_legend.py. Duplicated here so this script has
# no import-side dependencies on the backend package (runnable as a
# standalone one-shot).
LEGACY_TO_CANONICAL = {
    "alpha":    "camino",
    "camaro":   "barracuda",
    "chevelle": "hellcat",
    "redeye":   "gto",
}


async def main(apply_writes: bool) -> None:
    url = os.environ["MONGO_URL"]
    name = os.environ["DB_NAME"]
    client = AsyncIOMotorClient(url)
    db = client[name]
    coll = db["observation_receipts"]

    print(f"target: {name}.observation_receipts")
    print(f"mode:   {'APPLY (writes enabled)' if apply_writes else 'DRY-RUN (no writes)'}")
    print()

    total_touched = 0
    for legacy, canonical in LEGACY_TO_CANONICAL.items():
        count = await coll.count_documents({"brain": legacy})
        label = f"  {legacy:>8} → {canonical:<10}  {count:>5} rows"
        if count == 0:
            print(label)
            continue
        if apply_writes:
            result = await coll.update_many(
                {"brain": legacy},
                {"$set": {"brain": canonical, "brain_original_legacy": legacy}},
            )
            print(f"{label}   ← updated {result.modified_count}")
            total_touched += result.modified_count
        else:
            print(f"{label}   (would update — pass --apply to write)")
            total_touched += count

    print()
    print(f"Total rows {'updated' if apply_writes else 'would be updated'}: {total_touched}")

    client.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument(
        "--apply", action="store_true",
        help="Actually perform the writes. Default is dry-run.",
    )
    args = ap.parse_args()
    asyncio.run(main(args.apply))
