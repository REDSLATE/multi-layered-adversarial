"""Live index-maintenance endpoint.

Doctrine pin (2026-02-26, operator-discovered + post-mortem):

  * Startup MUST NOT block on heavyweight index builds. Lifespan now
    fires `ensure_indexes()` as a background task.
  * This admin endpoint is the operator-triggered path. It calls
    `ensure_indexes()` with a 60s per-index deadline (vs 6s at
    startup) and returns the full per-index report (created /
    exists / timeout / error) so the operator can see exactly which
    indexes are already in place and which are still building.

Mongo's `createIndex` is idempotent — repeat calls against an
already-built index are sub-50ms no-ops, which the report tags as
"exists" so the operator can distinguish "I just built this" from
"this was already here".
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query

from auth import get_current_user
from db import ensure_indexes, get_index_report


logger = logging.getLogger("risedual.db_admin")
router = APIRouter(prefix="/admin/db", tags=["db-admin"])


@router.post("/ensure-indexes")
async def ensure_indexes_now(
    deadline_s: float = Query(
        default=60.0, ge=5.0, le=300.0,
        description="per-index client-side deadline (default 60s)",
    ),
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    """Run `db.ensure_indexes()` against the live pod's Mongo
    connection with a 60s-per-index deadline. Idempotent. Returns
    a per-index status report:

      * `"status": "exists"`   — Mongo no-op'd (index was already built)
      * `"status": "created"`  — Mongo built it within the deadline
      * `"status": "timeout"`  — client gave up; Mongo continues building
                                in the BACKGROUND. Re-hit this endpoint in
                                a minute to see if it flipped to "exists".
      * `"status": "error"`    — see `reason` (operation failure, etc.)
    """
    started_at = datetime.now(timezone.utc)
    started_mono = asyncio.get_event_loop().time()
    try:
        await ensure_indexes(heavy_deadline_s=deadline_s)
        elapsed = asyncio.get_event_loop().time() - started_mono
        report = get_index_report()
        # Summary counters for quick visual scan.
        by_status: dict[str, int] = {}
        for r in report.values():
            by_status[r["status"]] = by_status.get(r["status"], 0) + 1
        logger.warning(
            "ensure_indexes() admin run; elapsed=%.2fs summary=%s",
            elapsed, by_status,
        )
        return {
            "ok": True,
            "elapsed_seconds": round(elapsed, 3),
            "deadline_s_per_index": deadline_s,
            "started_at": started_at.isoformat(),
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "summary_by_status": by_status,
            "per_index": report,
        }
    except Exception as exc:  # noqa: BLE001
        elapsed = asyncio.get_event_loop().time() - started_mono
        logger.exception("ensure_indexes() admin call failed")
        return {
            "ok": False,
            "error_type": type(exc).__name__,
            "error_message": str(exc)[:1000],
            "elapsed_seconds": round(elapsed, 3),
            "per_index_partial": get_index_report(),
        }
