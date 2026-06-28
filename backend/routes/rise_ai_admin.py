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
from shared.rise_ai.learning_loop import (
    SEATS,
    _harvest_one_seat,
    status as learning_loop_status,
)


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


# ─────────────── 2026-02-25 — learning-loop endpoints ───────────────


@router.get("/learning-loop/status")
async def learning_loop_status_endpoint(
    _user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    """Operator view of the periodic auto-grader + harvester. Returns
    enabled flag, running state of each task, intervals, and last-run
    receipts for both phases — including any error and the per-seat
    rows_written distribution from the last harvest tick."""
    snap = learning_loop_status()
    # Augment with current checkpoint metrics so the operator can see
    # corpus growth across cycles without joining tables in their head.
    seat_metrics: list[dict[str, Any]] = []
    async for row in db.ai_checkpoints.find(
        {"role": {"$in": list(SEATS)}, "state": {"$in": ["SHADOW", "ADVISOR", "PRIMARY"]}},
        {"_id": 0, "role": 1, "state": 1, "model_id": 1, "metrics": 1, "updated_at": 1},
    ):
        seat_metrics.append({
            "seat": row.get("role"),
            "state": row.get("state"),
            "model_id": row.get("model_id"),
            "rows_seeded": (row.get("metrics") or {}).get("rows_seeded"),
            "last_harvest_at": (row.get("metrics") or {}).get("last_harvest_at"),
            "updated_at": row.get("updated_at"),
        })
    snap["seat_checkpoints"] = seat_metrics
    return snap


@router.post("/learning-loop/harvest-now")
async def trigger_harvest_now(
    seat: str | None = Query(default=None, description="optional single-seat harvest"),
    _user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    """Manually run the harvester phase. With no `seat` argument,
    runs all 8 seats. With a seat name, runs just that one. Returns
    per-seat receipts. Does NOT trigger the grader — call
    `POST /admin/rise-ai/auto-grade` for that.

    Idempotent: harvester is a full-file rewrite each cycle, so
    running it twice in a row produces the same JSONL state."""
    if seat is not None and seat not in SEATS:
        return {"ok": False, "error": f"unknown seat {seat!r}", "known": list(SEATS)}
    seats_to_run = [seat] if seat else list(SEATS)
    per_seat = []
    for s in seats_to_run:
        per_seat.append(await _harvest_one_seat(s))
    total_rows = sum(p.get("rows_written") or 0 for p in per_seat)
    return {
        "ok": True,
        "seats_run": seats_to_run,
        "total_rows_written": total_rows,
        "per_seat": per_seat,
    }
