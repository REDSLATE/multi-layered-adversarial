"""Admin-only cleanup for non-executable intents (2026-07-03).

Doctrine: HOLD/WATCH intents that never fired are safe to purge —
they carry no trading history and can flood the operator's view.
Purge is dry-run BY DEFAULT so nothing gets deleted accidentally;
`confirm=true` is required to actually remove rows.

Hard invariants this endpoint enforces:
    * NEVER deletes `executed=true` — that's real trading history
    * NEVER deletes rows younger than `min_age_hours` (default 6h)
      so the pipeline can still process fresh intents
    * NEVER deletes anything with `broker_order_id` set — belt +
      suspenders in case `executed` wasn't stamped correctly
    * Restricted to HOLD/WATCH actions — BUY/SELL/SHORT/COVER
      unexecuted intents are left alone (they may be pending
      execution)
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query

from auth import get_current_user
from db import db

logger = logging.getLogger("risedual.intents_purge")
router = APIRouter(prefix="/admin/intents", tags=["intents-purge"])


@router.post("/purge-non-executable")
async def purge_non_executable_intents(
    _user: dict = Depends(get_current_user),
    min_age_hours: int = Query(
        default=6, ge=1, le=720,
        description="Only purge intents older than this. Default 6h.",
    ),
    lane: Optional[str] = Query(
        default=None,
        description="Restrict to a single lane (equity | crypto).",
    ),
    confirm: bool = Query(
        default=False,
        description="Dry-run unless True. Prevents accidental deletion.",
    ),
) -> dict:
    """Purge HOLD/WATCH intents that never fired.

    Safety-first design:
      * `confirm=false` (default) → returns what WOULD be deleted
        without touching anything
      * `confirm=true` → actually deletes
      * Query is compound: `action ∈ {HOLD, WATCH}` AND
        `executed=false` AND `ingest_ts < now - min_age_hours` AND
        (`broker_order_id` unset or null)
    """
    if lane is not None and lane not in ("equity", "crypto"):
        return {
            "ok": False,
            "error": (
                f"lane must be 'equity' or 'crypto' (or omitted); "
                f"got {lane!r}"
            ),
        }

    cutoff = (
        datetime.now(timezone.utc) - timedelta(hours=min_age_hours)
    ).isoformat()

    q: dict = {
        "action": {"$in": ["HOLD", "WATCH"]},
        "executed": {"$ne": True},
        "ingest_ts": {"$lt": cutoff},
        # Extra safety belt — anything that ever hit the broker
        # must have a broker_order_id; refuse to touch those.
        "$or": [
            {"broker_order_id": {"$exists": False}},
            {"broker_order_id": None},
        ],
    }
    if lane:
        q["lane"] = lane

    count = await db.shared_intents.count_documents(q)

    if not confirm:
        # Also expose a couple of sample IDs so the operator can
        # eyeball one before pulling the trigger.
        sample_cursor = db.shared_intents.find(
            q, projection={"intent_id": 1, "symbol": 1, "action": 1, "_id": 0},
        ).limit(3)
        samples = await sample_cursor.to_list(3)
        return {
            "ok": True,
            "dry_run": True,
            "would_delete": count,
            "sample_intents": samples,
            "cutoff_ts": cutoff,
            "note": (
                "This is a preview. Nothing was deleted. Pass "
                "`?confirm=true` (with the same filters) to actually purge."
            ),
        }

    result = await db.shared_intents.delete_many(q)
    logger.info(
        "purge_non_executable_intents: deleted=%s min_age_hours=%s lane=%s "
        "requested_by=%s",
        result.deleted_count, min_age_hours, lane or "any",
        _user.get("email"),
    )
    return {
        "ok": True,
        "dry_run": False,
        "deleted": result.deleted_count,
        "cutoff_ts": cutoff,
        "lane": lane,
        "requested_by": _user.get("email"),
    }
