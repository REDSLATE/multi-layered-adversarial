"""Admin endpoints for the stale-intent sweeper.

    POST /api/admin/intents/purge-stale
        Body: {dry_run: bool = true, batch_limit: int = 500}
        Runs one sweep pass and returns counts + samples. Safe by
        default — `dry_run=true` never touches Mongo.

    GET /api/admin/intents/sweeper/status
        Reports whether the background task is alive + config.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from auth import get_current_user
from db import db
from shared import intent_sweeper as _sweeper

logger = logging.getLogger("routes.intent_sweeper_admin")

router = APIRouter(prefix="/admin/intents", tags=["intent-sweeper"])


class PurgeStaleIn(BaseModel):
    dry_run: bool = Field(
        default=True,
        description=(
            "When True (default), no rows are touched. Use False to "
            "actually archive-then-delete."
        ),
    )
    batch_limit: int = Field(
        default=_sweeper.BATCH_LIMIT_DEFAULT,
        ge=1,
        le=_sweeper.BATCH_LIMIT_MAX,
        description="Max rows to process this call (hard cap 1000).",
    )


@router.post("/purge-stale")
async def purge_stale(
    body: Optional[PurgeStaleIn] = None,
    _user: dict = Depends(get_current_user),
) -> dict:
    """One-shot sweep. Returns counts + first 5 candidate samples.

    Doctrine (2026-02-19 operator directive):
        Archive stale intents older than 6h that never reached the
        broker. If a resolved learning_experience exists for the
        intent, DELETE outright (knowledge distilled). Otherwise,
        archive to `shared_intents_archive` first, verify the
        write, then delete from hot.

    Dry-run first. Then flip `dry_run=false` when the sample looks
    correct.
    """
    body = body or PurgeStaleIn()
    counts = await _sweeper.sweep_stale_intents(
        db,
        dry_run=body.dry_run,
        batch_limit=body.batch_limit,
    )
    return {"ok": True, **counts}


@router.get("/sweeper/status")
async def sweeper_status(_user: dict = Depends(get_current_user)) -> dict:
    """Liveness + config for the scheduled sweeper loop."""
    task = _sweeper._TASK  # noqa: SLF001 — read-only telemetry
    return {
        "ok": True,
        "enabled_env": _sweeper.SWEEPER_ENABLED,
        "task_alive": bool(task and not task.done()),
        "task_done": bool(task and task.done()),
        "task_exception": (
            repr(task.exception()) if (task and task.done()
                                       and task.exception()) else None
        ),
        "interval_sec": _sweeper.INTERVAL_SEC,
        "min_age_hours": _sweeper.MIN_AGE_HOURS,
        "batch_limit_default": _sweeper.BATCH_LIMIT_DEFAULT,
        "batch_limit_max": _sweeper.BATCH_LIMIT_MAX,
        "archive_collection": _sweeper.SHARED_INTENTS_ARCHIVE,
        "archive_version": _sweeper.ARCHIVE_VERSION,
        "doctrine_note": (
            "Archives intents older than 6h that never reached the "
            "broker. Learned intents (with a resolved learning "
            "experience) are deleted outright — no archive — since "
            "the learning tape now carries the signal."
        ),
    }
