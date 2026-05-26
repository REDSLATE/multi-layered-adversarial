"""Backfill `memory_id` / `decision_id` FKs onto `shared_labeled_memories`.

Schema-tightening 2026-05-25:
    `shared_labeled_memories` historically embedded the memory's
    decision id inside `payload_summary` / `reason` as
    `decision_id=<id>` substrings. As of 2026-05-25, new emitters
    write `memory_id` + `decision_id` as top-level FK fields.

    This script walks every legacy row missing the FK, regex-parses
    the legacy fields, and backstamps the top-level keys. After this
    runs across all brains' historical labels, the regex fallback in
    `runtime_cross_brain_memories._quarantined_memory_ids` can be
    deleted.

Idempotency:
    * Rows that ALREADY carry `memory_id` are skipped.
    * Rows where the regex finds nothing are recorded as "unmatched"
      so the operator can decide whether to re-label them manually.
    * Multi-run safe — re-running is a no-op once the corpus is
      backfilled.

Usage:
    cd /app/backend
    python scripts/backfill_memory_label_fk.py
    python scripts/backfill_memory_label_fk.py --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import re

from db import db
from namespaces import SHARED_MEMORY


logger = logging.getLogger(__name__)


_DECISION_ID_RE = re.compile(r"decision_id=([A-Za-z0-9_-]+)", re.IGNORECASE)
_MEMORY_ID_RE = re.compile(r"memory_id=([A-Za-z0-9_-]+)", re.IGNORECASE)


async def backfill(dry_run: bool = False) -> dict:
    """Walk every legacy row missing a memory_id FK and backstamp.

    Returns counts so the operator (or a tripwire) can verify the
    migration succeeded.
    """
    scanned = 0
    backfilled = 0
    unmatched = 0
    already_done = 0

    cursor = db[SHARED_MEMORY].find(
        {},
        {"_id": 1, "memory_id": 1, "decision_id": 1,
         "payload_summary": 1, "reason": 1, "label": 1},
    )

    async for row in cursor:
        scanned += 1
        if row.get("memory_id") or row.get("decision_id"):
            already_done += 1
            continue

        mid = None
        did = None
        for field in ("payload_summary", "reason"):
            val = str(row.get(field) or "")
            if did is None:
                m = _DECISION_ID_RE.search(val)
                if m:
                    did = m.group(1).strip()
            if mid is None:
                m = _MEMORY_ID_RE.search(val)
                if m:
                    mid = m.group(1).strip()
            if mid and did:
                break

        if not mid and not did:
            unmatched += 1
            continue

        if not dry_run:
            update_doc: dict = {}
            if mid:
                update_doc["memory_id"] = mid
            if did:
                update_doc["decision_id"] = did
            update_doc["fk_backfilled_at"] = "2026-05-25_migration"
            await db[SHARED_MEMORY].update_one(
                {"_id": row["_id"]},
                {"$set": update_doc},
            )
        backfilled += 1

    summary = {
        "scanned": scanned,
        "already_done": already_done,
        "backfilled": backfilled,
        "unmatched": unmatched,
        "dry_run": dry_run,
    }
    logger.info("backfill summary: %s", summary)
    return summary


def _parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dry-run", action="store_true",
                   help="report counts without writing")
    return p.parse_args()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    args = _parse_args()
    result = asyncio.run(backfill(dry_run=args.dry_run))
    print(result)
