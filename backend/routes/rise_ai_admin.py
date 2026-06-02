"""Admin endpoints for the RISE AI auto-grader.

Mounted under `/api/admin/rise-ai/` so an operator can fire a grading
pass without opening a shell.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from auth import get_current_user
from db import db
from shared.rise_ai.auto_grader import grade_batch


router = APIRouter(prefix="/admin/rise-ai", tags=["rise-ai"])


@router.post("/auto-grade")
async def trigger_auto_grade(
    limit: int = Query(50, ge=1, le=500),
    _user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    """Grade up to `limit` ungraded llm_calls rows. Cost-bounded by
    `limit` (max 500). Returns a summary the UI can render."""
    summary = await grade_batch(db, limit=limit)
    return {"ok": True, **summary}


@router.get("/grading-stats")
async def grading_stats(
    _user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    """Operator dashboard tile data: how many ungraded rows remain by
    role, plus grade=1 vs grade=0 counts so the operator can see if
    the corpus is growing."""
    total = await db.llm_calls.count_documents({})
    ungraded = await db.llm_calls.count_documents({"grade": {"$exists": False}})
    g1 = await db.llm_calls.count_documents({"grade": 1})
    g0 = await db.llm_calls.count_documents({"grade": 0})

    by_role: list[dict[str, Any]] = []
    pipeline = [
        {"$match": {"grade": {"$exists": False}}},
        {"$group": {"_id": "$role", "n": {"$sum": 1}}},
        {"$sort": {"n": -1}},
    ]
    async for row in db.llm_calls.aggregate(pipeline):
        by_role.append({"role": row.get("_id") or "unknown", "ungraded": row["n"]})

    return {
        "ok": True,
        "totals": {"total": total, "ungraded": ungraded, "g1": g1, "g0": g0},
        "ungraded_by_role": by_role,
    }
