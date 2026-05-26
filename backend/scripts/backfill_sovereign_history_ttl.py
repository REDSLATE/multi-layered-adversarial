"""Backfill `received_at_dt` BSON Date on `sovereign_state_history`.

Storage-tightening 2026-05-26:
    A 30d TTL index was added to `sovereign_state_history` keyed on
    `received_at_dt` (BSON Date). New rows written after the migration
    carry the field automatically (see
    `shared/sovereign_mode_guard.py`). Legacy rows have only the ISO
    string `received_at` field, so the TTL never fires on them.

    This script walks every legacy row missing `received_at_dt`,
    parses `received_at` (or `ts`, or the implicit `_id` timestamp as
    last resort), and stamps the Date so TTL picks them up on the
    next sweep.

Idempotent: re-running is a no-op once every row has the field.

Usage:
    cd /app/backend
    python scripts/backfill_sovereign_history_ttl.py
    python scripts/backfill_sovereign_history_ttl.py --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import datetime, timezone

from db import db
from namespaces import SOVEREIGN_STATE_HISTORY


logger = logging.getLogger(__name__)


def _parse_iso(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value:
        return None
    try:
        # Handle Z suffix and microseconds.
        v = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(v)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


async def backfill(dry_run: bool = False) -> dict:
    scanned = 0
    already_done = 0
    backfilled = 0
    unparseable = 0

    cursor = db[SOVEREIGN_STATE_HISTORY].find(
        {},
        {"_id": 1, "received_at": 1, "ts": 1, "received_at_dt": 1},
    )
    async for row in cursor:
        scanned += 1
        if isinstance(row.get("received_at_dt"), datetime):
            already_done += 1
            continue
        dt = _parse_iso(row.get("received_at")) or _parse_iso(row.get("ts"))
        if dt is None:
            # Final fallback — derive from ObjectId's embedded timestamp.
            oid = row.get("_id")
            try:
                dt = oid.generation_time  # bson.ObjectId attribute
            except AttributeError:
                dt = None
        if dt is None:
            unparseable += 1
            continue
        if not dry_run:
            await db[SOVEREIGN_STATE_HISTORY].update_one(
                {"_id": row["_id"]},
                {"$set": {"received_at_dt": dt,
                          "ttl_backfilled_at": "2026-05-26"}},
            )
        backfilled += 1

    summary = {
        "scanned": scanned,
        "already_done": already_done,
        "backfilled": backfilled,
        "unparseable": unparseable,
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
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    args = _parse_args()
    result = asyncio.run(backfill(dry_run=args.dry_run))
    print(result)
