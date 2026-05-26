"""Drop the legacy `sovereign_history_ttl_30d` index.

Doctrine (2026-05-26):
    `sovereign_state_history` is being converted from TTL-DELETE to
    storage-rollup. The 60d rollup pipeline preserves
    `{movement, event, mode, delta_was_clamped, …}` labels in a slim
    `sovereign_state_history_rollups` collection, then purges the
    verbose original after a 7d hold.

    The previous 30d TTL-delete index conflicts with this — it would
    keep deleting rows before the rollup pipeline can label them. This
    script removes that index. Idempotent: safe to re-run.

Usage:
    cd /app/backend
    python scripts/drop_sovereign_history_ttl.py
    python scripts/drop_sovereign_history_ttl.py --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import logging

from db import db


logger = logging.getLogger(__name__)

INDEX_NAME = "sovereign_history_ttl_30d"


async def drop_ttl(dry_run: bool = False) -> dict:
    coll = db["sovereign_state_history"]
    info = await coll.index_information()
    present = INDEX_NAME in info
    if not present:
        return {"index": INDEX_NAME, "present": False, "dropped": False, "dry_run": dry_run}
    if dry_run:
        return {"index": INDEX_NAME, "present": True, "dropped": False, "dry_run": True}
    await coll.drop_index(INDEX_NAME)
    return {"index": INDEX_NAME, "present": True, "dropped": True, "dry_run": False}


def _parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dry-run", action="store_true")
    return p.parse_args()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    args = _parse_args()
    result = asyncio.run(drop_ttl(dry_run=args.dry_run))
    print(result)
