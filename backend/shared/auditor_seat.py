"""Auditor seat registry — mirrors `executor_seat.py`.

Doctrine:
  * Exactly ONE brain holds the auditor seat at any time. Default: NONE.
  * The seat ROTATES. Any of the four brains can be assigned. The brain
    holding this seat plays the contrary-case voice in every hypothesis
    analysis — "what could go wrong, when do we kill it, what we'd hate
    to be wrong about."
  * Empty by default. Operator must assign before any hypothesis carries
    an Auditor narrative (it'll fall back to a generic skeptic prompt).

This is intentionally a SEPARATE registry from the Executor seat — the
two roles can be held simultaneously by different brains (and usually
should be).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from auth import get_current_user
from db import db
from namespaces import (
    RUNTIMES,
    SHARED_AUDITOR_ROTATIONS,
    SHARED_AUDITOR_SEAT,
)


router = APIRouter(tags=["auditor"])

_SEAT_DOC_ID = "auditor"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def get_auditor_holder() -> Optional[str]:
    doc = await db[SHARED_AUDITOR_SEAT].find_one(
        {"_id": _SEAT_DOC_ID}, {"_id": 0, "holder": 1}
    )
    if not doc:
        return None
    h = doc.get("holder")
    return h if h in RUNTIMES else None


class RotateBody(BaseModel):
    new_holder: Optional[
        Literal["alpha", "camaro", "chevelle", "redeye", "", None]
    ] = Field(default=None, description="brain name, or null/empty to clear seat")
    reason: str = Field(min_length=3, max_length=1000)


@router.get("/auditor")
async def get_auditor_state():
    doc = await db[SHARED_AUDITOR_SEAT].find_one(
        {"_id": _SEAT_DOC_ID}, {"_id": 0}
    )
    if not doc:
        return {
            "holder": None,
            "since": None,
            "assigned_by": None,
            "reason": "empty",
            "default": True,
        }
    return {**doc, "default": doc.get("holder") is None}


@router.post("/auditor/rotate")
async def rotate_auditor(
    body: RotateBody,
    user: dict = Depends(get_current_user),  # noqa: B008
):
    new_holder = body.new_holder or None
    if new_holder is not None and new_holder not in RUNTIMES:
        raise HTTPException(
            status_code=422,
            detail=f"new_holder must be one of {sorted(RUNTIMES)} or null",
        )
    previous = await get_auditor_holder()
    if previous == new_holder:
        raise HTTPException(
            status_code=400,
            detail=f"auditor seat already held by {previous or 'nobody'}; nothing to rotate",
        )
    now = _now_iso()
    seat_doc = {
        "_id": _SEAT_DOC_ID,
        "holder": new_holder,
        "since": now if new_holder else None,
        "assigned_by": user.get("email") if new_holder else None,
        "reason": body.reason,
        "last_rotated_at": now,
    }
    await db[SHARED_AUDITOR_SEAT].replace_one(
        {"_id": _SEAT_DOC_ID}, seat_doc, upsert=True
    )
    rotation = {
        "rotation_id": str(uuid.uuid4()),
        "previous_holder": previous,
        "new_holder": new_holder,
        "by_admin_email": user.get("email"),
        "reason": body.reason,
        "ts": now,
    }
    await db[SHARED_AUDITOR_ROTATIONS].insert_one(rotation)
    return {
        "ok": True,
        "previous_holder": previous,
        "new_holder": new_holder,
        "since": seat_doc["since"],
        "reason": body.reason,
        "rotation_id": rotation["rotation_id"],
        "rotated_by": user.get("email"),
        "ts": now,
    }


@router.get("/auditor/audit")
async def auditor_audit(
    limit: int = Query(default=50, ge=1, le=500),
):
    rows = (
        await db[SHARED_AUDITOR_ROTATIONS]
        .find({}, {"_id": 0})
        .sort("ts", -1)
        .to_list(limit)
    )
    return {"items": rows, "count": len(rows)}
