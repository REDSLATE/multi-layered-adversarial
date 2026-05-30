"""Seat-holder nudges — operator pings the brain currently holding a
silent/missing seat on a specific position.

Doctrine (2026-05-30):
  ADVISORY OBSERVABILITY ONLY. A nudge does not:
    - Force a seat reassignment
    - Veto an intent
    - Modify execution authority
    - Affect any gate decision

  The brain reads its nudges via runtime-token (poll-friendly).
  Cooldown-throttled per (position_id, seat) to prevent spam.

  The nudge address resolves at SEND TIME — whichever brain holds
  the seat at the moment of the operator click receives it. Same
  position-model authority pattern as `_compute_quorum`: identity
  is irrelevant, the seat IS the address.

Endpoints:
    POST /api/admin/positions/{position_id}/nudge-seat
        Operator → MC. Body: {seat, message?}. Validates the seat is
        currently held by a brain (404 if vacant) and that the cooldown
        window has elapsed (429 with retry_after_seconds). Writes a
        nudge row and returns it.

    GET  /api/admin/positions/{position_id}/nudges
        Operator → MC. Read recent nudges for a position. Newest first.

    GET  /api/runtime-discussion/seat-nudges?runtime={brain}&since={iso}
        Brain → MC (runtime-token auth). Returns this brain's recent
        unacknowledged nudges. `since` is optional ISO timestamp; brain
        can pass the last `ts` it saw to avoid re-reading. Brain may
        also POST an ack (next iteration).

Storage:
    seat_nudges — append-only:
        {
          nudge_id, position_id, seat, brain, sent_by_email,
          message, ts, ts_epoch, status: "sent"
        }
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Header, Query
from pydantic import BaseModel, Field

from auth import get_current_user
from db import db
from namespaces import (
    DISCUSSION_PARTICIPANTS,
    SEAT_NUDGES,
    SHARED_POSITIONS,
)
from runtime_auth import verify_runtime_token
from shared.roster import get_roster
from shared.seat_policy import required_seats


router = APIRouter(tags=["seat-nudges"])

# Default cooldown — same band as opinion_silence_watchdog's
# (30 min) to keep operator throttling consistent.
NUDGE_COOLDOWN_SEC = 30 * 60

# Max recent nudges to surface per fetch (both operator + brain paths).
MAX_NUDGES_RETURNED = 50


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_epoch() -> float:
    return datetime.now(timezone.utc).timestamp()


class NudgeBody(BaseModel):
    seat: str = Field(min_length=1, max_length=64)
    message: Optional[str] = Field(default=None, max_length=512)


async def _recent_nudge(position_id: str, seat: str, within_sec: int) -> Optional[dict]:
    cutoff = _now_epoch() - within_sec
    return await db[SEAT_NUDGES].find_one(
        {
            "position_id": position_id,
            "seat": seat,
            "ts_epoch": {"$gte": cutoff},
        },
        {"_id": 0},
        sort=[("ts_epoch", -1)],
    )


@router.post("/admin/positions/{position_id}/nudge-seat")
async def nudge_seat(
    position_id: str,
    body: NudgeBody,
    user: dict = Depends(get_current_user),  # noqa: B008
):
    """Operator pings the current holder of `seat` on `position_id`.

    422 — `seat` is not a recognized required seat.
    404 — position not found, OR seat is currently vacant (no one to ping).
    429 — cooldown active; returns `retry_after_seconds`.
    200 — nudge recorded; returns the row.

    Doctrine: ADVISORY ONLY. The nudge is delivered passively (brain
    polls `/api/runtime-discussion/seat-nudges`). MC does not push,
    does not retry, does not escalate.
    """
    seat = body.seat.lower().strip()
    valid_seats = set(required_seats())
    if seat not in valid_seats:
        raise HTTPException(
            status_code=422,
            detail=(
                f"seat {seat!r} is not a recognized required seat. "
                f"Valid: {sorted(valid_seats)}"
            ),
        )

    pos = await db[SHARED_POSITIONS].find_one(
        {"position_id": position_id}, {"_id": 0, "position_id": 1, "symbol": 1, "state": 1},
    )
    if not pos:
        raise HTTPException(status_code=404, detail=f"position {position_id} not found")

    roster = await get_roster()
    assignments = roster.get("assignments") or {}
    current_holder = assignments.get(seat)
    if not current_holder:
        raise HTTPException(
            status_code=404,
            detail=(
                f"seat {seat!r} is currently vacant — no brain to nudge. "
                f"Assign a holder via the roster first."
            ),
        )

    # Cooldown check — same (position, seat) within window.
    prev = await _recent_nudge(position_id, seat, NUDGE_COOLDOWN_SEC)
    if prev:
        retry_after = max(
            1,
            int(NUDGE_COOLDOWN_SEC - (_now_epoch() - float(prev["ts_epoch"]))),
        )
        raise HTTPException(
            status_code=429,
            detail={
                "blocked_by": "nudge_cooldown",
                "reason": (
                    f"seat {seat!r} on position {position_id} was nudged "
                    f"{int(_now_epoch() - float(prev['ts_epoch']))}s ago "
                    f"(cooldown {NUDGE_COOLDOWN_SEC}s)"
                ),
                "retry_after_seconds": retry_after,
                "last_nudge": prev,
            },
        )

    nudge = {
        "nudge_id": str(uuid.uuid4()),
        "position_id": position_id,
        "symbol": pos.get("symbol"),
        "seat": seat,
        "brain": current_holder,
        "sent_by_email": user.get("email") or "operator",
        "message": (body.message or "").strip() or None,
        "ts": _now_iso(),
        "ts_epoch": _now_epoch(),
        "status": "sent",
        "authority": "advisory_observability_only",
    }
    await db[SEAT_NUDGES].insert_one(nudge)
    nudge.pop("_id", None)
    return {"ok": True, "nudge": nudge}


@router.get("/admin/positions/{position_id}/nudges")
async def list_nudges_for_position(
    position_id: str,
    limit: int = Query(default=MAX_NUDGES_RETURNED, ge=1, le=200),
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    rows = await db[SEAT_NUDGES].find(
        {"position_id": position_id}, {"_id": 0},
    ).sort("ts_epoch", -1).to_list(limit)
    return {"items": rows, "count": len(rows)}


@router.get("/runtime-discussion/seat-nudges")
async def list_nudges_for_brain(
    runtime: str = Query(..., description="brain name"),
    since: Optional[str] = Query(
        default=None,
        description="ISO timestamp; only return nudges with ts > this value",
    ),
    limit: int = Query(default=MAX_NUDGES_RETURNED, ge=1, le=200),
    x_runtime_token: str = Header(default=""),
):
    """Brain-callable. Returns nudges addressed to `runtime`. The brain
    polls this with `since` set to the last nudge timestamp it processed.
    """
    verify_runtime_token(runtime, x_runtime_token)
    q: dict = {"brain": runtime}
    if since:
        q["ts"] = {"$gt": since}
    rows = await db[SEAT_NUDGES].find(q, {"_id": 0}).sort("ts_epoch", -1).to_list(limit)
    return {"runtime": runtime, "since": since, "count": len(rows), "items": rows}
