"""Live index-maintenance endpoint.

Doctrine pin (2026-02-26, operator-discovered): The auto_router_loop
was stalling on every tick with `MaxTimeMSExpired` because
`shared_intents` was missing the `(action, created_at)` compound index
the auto-router's `_tick()` query needs. The standard fix path is:

    1. Add the index to `db.ensure_indexes()`
    2. Push a new build
    3. Wait for the pod to roll over and create it at startup

That's an entire deploy cycle just to add an index. This endpoint
short-circuits step 3 — the operator hits one curl and Mongo starts
building the missing indexes against the running pod's collections.
Subsequent ticks pick up the new index automatically; no restart
needed.

Mongo's `createIndex` is idempotent — if the index already exists with
the same spec, the call is a no-op. So this is safe to hit at any time,
even multiple times in a row.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends

from auth import get_current_user
from db import ensure_indexes


logger = logging.getLogger("risedual.db_admin")
router = APIRouter(prefix="/admin/db", tags=["db-admin"])


@router.post("/ensure-indexes")
async def ensure_indexes_now(_user: dict = Depends(get_current_user)):  # noqa: B008
    """Run `db.ensure_indexes()` against the live pod's Mongo
    connection. Idempotent. Use this when a new index has been added
    to `db.py` and you want it built *without* waiting for the next
    deploy/restart.

    Returns timing data so the operator knows whether the call
    actually did any work (fast = no-ops, slow = index built)."""
    started_at = datetime.now(timezone.utc)
    started_mono = asyncio.get_event_loop().time()
    try:
        await ensure_indexes()
        elapsed = asyncio.get_event_loop().time() - started_mono
        logger.warning(
            "ensure_indexes() run via admin endpoint; elapsed=%.2fs",
            elapsed,
        )
        return {
            "ok": True,
            "elapsed_seconds": round(elapsed, 3),
            "started_at": started_at.isoformat(),
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "note": (
                "Idempotent. Slow runs (>5s) typically mean a new "
                "index was built; fast runs (<1s) mean all indexes "
                "already existed."
            ),
        }
    except Exception as exc:  # noqa: BLE001
        elapsed = asyncio.get_event_loop().time() - started_mono
        logger.exception("ensure_indexes() admin call failed")
        return {
            "ok": False,
            "error_type": type(exc).__name__,
            "error_message": str(exc)[:1000],
            "elapsed_seconds": round(elapsed, 3),
        }
