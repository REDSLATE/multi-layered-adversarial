"""Admin read/inspect surface for the learning loop (Stage 1).

Endpoints:
    GET  /api/admin/learning/stats
        Global counts + resolver backlog snapshot.

    GET  /api/admin/learning/experiences?limit=20&lane=X&resolved=only|none
        Recent experiences, filterable by lane + resolution state.

    POST /api/admin/learning/resolve
        Manually kick a resolver sweep (returns the counts dict).
        Useful before wiring the resolver into a scheduler.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query

from auth import get_current_user
from db import db
from shared.learning.live_loop import LEARNING_EXPERIENCES
from shared.learning.outcome_resolver import resolve_pending_outcomes

router = APIRouter(prefix="/admin/learning", tags=["admin", "learning"])


@router.get("/stats")
async def learning_stats(_user: dict = Depends(get_current_user)) -> dict:
    """One-call overview of the learning tape's health."""
    total = await db[LEARNING_EXPERIENCES].count_documents({})
    pending_5m = await db[LEARNING_EXPERIENCES].count_documents(
        {"outcome_5m_bps": None},
    )
    pending_15m = await db[LEARNING_EXPERIENCES].count_documents(
        {"outcome_15m_bps": None},
    )
    pending_1h = await db[LEARNING_EXPERIENCES].count_documents(
        {"outcome_1h_bps": None},
    )
    wins = await db[LEARNING_EXPERIENCES].count_documents({"win": True})
    losses = await db[LEARNING_EXPERIENCES].count_documents({"win": False})
    by_lane: dict[str, int] = {}
    async for row in db[LEARNING_EXPERIENCES].aggregate([
        {"$group": {"_id": "$lane", "n": {"$sum": 1}}},
    ]):
        by_lane[row["_id"] or "unknown"] = row["n"]
    return {
        "ok": True,
        "total_experiences": total,
        "pending_outcomes": {
            "5m": pending_5m, "15m": pending_15m, "1h": pending_1h,
        },
        "wins_5m": wins,
        "losses_5m": losses,
        "hit_rate_5m": (
            (wins / (wins + losses)) if (wins + losses) else None
        ),
        "by_lane": by_lane,
    }


@router.get("/experiences")
async def list_experiences(
    limit: int = Query(default=20, ge=1, le=200),
    lane: Optional[str] = Query(default=None),
    resolved: Optional[str] = Query(
        default=None, pattern="^(only|none)$",
    ),
    _user: dict = Depends(get_current_user),
) -> dict:
    """Recent experiences, newest-first. Filters:
        lane      → 'equity' | 'crypto'
        resolved  → 'only' (5m field set) | 'none' (5m field null)
    """
    q: dict = {}
    if lane:
        q["lane"] = lane.lower()
    if resolved == "only":
        q["outcome_5m_bps"] = {"$ne": None}
    elif resolved == "none":
        q["outcome_5m_bps"] = None

    rows = []
    async for row in (
        db[LEARNING_EXPERIENCES]
        .find(q, {"_id": 0, "features": 0, "doctrine": 0})
        .sort("created_at", -1)
        .limit(limit)
    ):
        rows.append(row)
    return {"ok": True, "count": len(rows), "items": rows}


@router.post("/resolve")
async def kick_resolver(_user: dict = Depends(get_current_user)) -> dict:
    """Trigger one resolver sweep. Returns the counts dict."""
    counts = await resolve_pending_outcomes(db)
    return {"ok": True, "counts": counts}


# ═══════════════════════════════════════════════════════════════════
# STAGE 2: BUCKETS + LESSONS
# ═══════════════════════════════════════════════════════════════════
from shared.learning.bucket_analyzer import (  # noqa: E402
    LEARNING_BUCKETS, rebuild_buckets,
)
from shared.learning.lesson_proposer import (  # noqa: E402
    LEARNING_LESSONS, propose_lessons,
)


@router.post("/analyze")
async def kick_analyzer(_user: dict = Depends(get_current_user)) -> dict:
    """Full Stage 2 sweep: rebuild buckets → propose lessons.
    Returns both counts dicts so operators see the full pipeline in
    one call."""
    b_counts = await rebuild_buckets(db)
    l_counts = await propose_lessons(db)
    return {"ok": True, "buckets": b_counts, "lessons": l_counts}


@router.get("/buckets")
async def list_buckets(
    limit: int = Query(default=50, ge=1, le=500),
    min_samples: int = Query(default=0, ge=0),
    _user: dict = Depends(get_current_user),
) -> dict:
    """Recent buckets, sorted by sample count desc. Excludes buckets
    with fewer than `min_samples` observations."""
    q = {"samples": {"$gte": min_samples}} if min_samples else {}
    rows = []
    async for row in (
        db[LEARNING_BUCKETS].find(q)
        .sort("samples", -1).limit(limit)
    ):
        rows.append(row)
    return {"ok": True, "count": len(rows), "items": rows}


@router.get("/lessons")
async def list_lessons(
    state: Optional[str] = Query(
        default=None, pattern="^(proposed|approved|rejected|applied)$",
    ),
    limit: int = Query(default=50, ge=1, le=500),
    _user: dict = Depends(get_current_user),
) -> dict:
    """List learning lessons, optionally filtered by state."""
    q = {"state": state} if state else {}
    rows = []
    async for row in (
        db[LEARNING_LESSONS].find(q)
        .sort("proposed_at", -1).limit(limit)
    ):
        rows.append(row)
    return {"ok": True, "count": len(rows), "items": rows}


@router.post("/lessons/{lesson_id}/approve")
async def approve_lesson(
    lesson_id: str,
    user: dict = Depends(get_current_user),
) -> dict:
    """Kernel review — mark a proposed lesson as `approved`. Nothing
    self-applies to doctrine; approval is a human signal that this
    lesson's evidence is trustworthy enough to fold into the next
    doctrine iteration."""
    actor = (user or {}).get("email") or "operator"
    r = await db[LEARNING_LESSONS].update_one(
        {"_id": lesson_id, "state": "proposed"},
        {"$set": {
            "state": "approved",
            "approved_at": datetime.now(timezone.utc).isoformat(),
            "approved_by": actor,
        }},
    )
    if r.matched_count == 0:
        return {"ok": False, "reason": "lesson_not_found_or_not_proposed"}
    return {"ok": True, "lesson_id": lesson_id, "new_state": "approved"}


@router.post("/lessons/{lesson_id}/reject")
async def reject_lesson(
    lesson_id: str,
    user: dict = Depends(get_current_user),
) -> dict:
    """Kernel review — reject a lesson. State becomes `rejected`;
    the lesson will NOT be re-emitted for the same bucket even if
    the evidence changes (idempotent proposer sees existing doc)."""
    actor = (user or {}).get("email") or "operator"
    r = await db[LEARNING_LESSONS].update_one(
        {"_id": lesson_id, "state": "proposed"},
        {"$set": {
            "state": "rejected",
            "rejected_at": datetime.now(timezone.utc).isoformat(),
            "rejected_by": actor,
        }},
    )
    if r.matched_count == 0:
        return {"ok": False, "reason": "lesson_not_found_or_not_proposed"}
    return {"ok": True, "lesson_id": lesson_id, "new_state": "rejected"}
