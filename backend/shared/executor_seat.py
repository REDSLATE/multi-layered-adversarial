"""Executor seat registry — the only authority that may route orders.

Doctrine:
  * Exactly ONE brain holds the executor seat at any time. Default: NONE.
  * The seat ROTATES. Any of the four brains can be assigned. Identity is
    not destiny — Camaro is not "the executor"; Camaro may HOLD the
    executor seat for a window.
  * Empty by default after every deploy. The seat must be deliberately
    assigned by an operator before any order can route.
  * Rotation requires admin auth + a free-form reason; every rotation is
    appended to `shared_executor_rotations` (immutable audit).
  * `live_trading_enabled` is a separate guard. Assigning a seat does NOT
    enable live trading. That requires a dual-sign promotion (Week 3-4).

Storage:
  shared_executor_seat — single row with _id="executor", holding:
    { holder, since, assigned_by, reason }
    or { holder: None, since: None, assigned_by: None, reason: "empty" }

  shared_executor_rotations — append-only:
    { rotation_id, previous_holder, new_holder, by_admin_email, reason, ts }

Endpoints:
  GET  /api/executor                — current holder + history (peek)
  POST /api/executor/rotate         — admin-only, rotate or clear the seat
  GET  /api/executor/audit          — full rotation log

Helpers (importable from gates/broker code):
  get_executor_holder()             — async, returns str or None
  is_executor(brain)                — async, True iff brain currently holds it
  require_executor(brain)           — async, raises 403 if brain doesn't hold it
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
    SHARED_EXECUTOR_ROTATIONS,
    SHARED_EXECUTOR_SEAT,
)


router = APIRouter(tags=["executor"])

_SEAT_DOC_ID = "executor"  # single-row registry, _id = "executor"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ──────────────────────────── helpers ────────────────────────────

async def get_executor_holder() -> Optional[str]:
    """Return the current holder name, or None if seat is empty."""
    doc = await db[SHARED_EXECUTOR_SEAT].find_one(
        {"_id": _SEAT_DOC_ID}, {"_id": 0, "holder": 1}
    )
    if not doc:
        return None
    h = doc.get("holder")
    return h if h in RUNTIMES else None


async def is_executor(brain: str) -> bool:
    holder = await get_executor_holder()
    return holder is not None and holder == brain


async def require_executor(brain: str) -> None:
    """Raise 403 unless `brain` currently holds the executor seat."""
    holder = await get_executor_holder()
    if holder is None:
        raise HTTPException(
            status_code=403,
            detail="executor seat is empty — no brain may execute until operator assigns",
        )
    if holder != brain:
        raise HTTPException(
            status_code=403,
            detail=f"executor seat held by {holder}; {brain} cannot execute",
        )


# ──────────────────────────── schema ────────────────────────────

class RotateBody(BaseModel):
    new_holder: Optional[
        Literal["alpha", "camaro", "chevelle", "redeye", "", None]
    ] = Field(default=None, description="brain name, or null/empty to clear seat")
    reason: str = Field(min_length=3, max_length=1000)


# ──────────────────────────── routes ────────────────────────────

@router.get("/executor")
async def get_executor_state():
    """Public read of seat state. Tier-agnostic: any reader can see who
    holds the chair, but only operator can rotate."""
    doc = await db[SHARED_EXECUTOR_SEAT].find_one(
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


@router.post("/executor/rotate")
async def rotate_executor(
    body: RotateBody,
    user: dict = Depends(get_current_user),  # noqa: B008
):
    """Rotate or clear the executor seat. Admin-authenticated."""
    new_holder = body.new_holder or None
    if new_holder is not None and new_holder not in RUNTIMES:
        raise HTTPException(
            status_code=422,
            detail=f"new_holder must be one of {sorted(RUNTIMES)} or null",
        )

    previous = await get_executor_holder()

    if previous == new_holder:
        raise HTTPException(
            status_code=400,
            detail=f"executor seat already held by {previous or 'nobody'}; nothing to rotate",
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
    await db[SHARED_EXECUTOR_SEAT].replace_one(
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
    await db[SHARED_EXECUTOR_ROTATIONS].insert_one(rotation)

    # Strip the Mongo _id from the seat_doc before returning.
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


@router.get("/executor/audit")
async def executor_audit(
    limit: int = Query(default=50, ge=1, le=500),
):
    """Append-only rotation history. Newest first."""
    rows = (
        await db[SHARED_EXECUTOR_ROTATIONS]
        .find({}, {"_id": 0})
        .sort("ts", -1)
        .to_list(limit)
    )
    return {"items": rows, "count": len(rows)}
