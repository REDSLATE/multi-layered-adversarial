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
