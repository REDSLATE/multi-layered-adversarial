"""
LLM Ledger — operator-facing read + grade surface.

Doctrine pin (2026-02-XX):
    Grades route ONLY into the training pipeline (preference_log →
    distillation_queue). They do NOT affect execution. They do NOT
    promote providers. ADVISORY_ONLY remains locked on every row.

Endpoints (mounted under `/api/admin/llm/`):
    GET  /ledger                        — paginated list (preview-only)
    GET  /ledger/{call_id}              — full prompt + response
    POST /ledger/{call_id}/grade        — body: {score, outcome, note?}
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel, Field

from auth import get_current_user
from db import db
from namespaces import LLM_CALLS, LLM_PREFERENCE_LOG
from shared.llm.training.distillation_queue import (
    MIN_ENQUEUE_SCORE,
    enqueue_training_pair,
)
from shared.llm.training.preference_log import VALID_SCORES, record_preference


router = APIRouter(prefix="/admin/llm", tags=["llm-ledger"])


PREVIEW_CHARS = 200


def _preview(s: Optional[str]) -> str:
    if not s:
        return ""
    s = s.strip()
    return s if len(s) <= PREVIEW_CHARS else f"{s[:PREVIEW_CHARS - 1]}…"


def _serialize_row(doc: Dict[str, Any], *, full: bool = False) -> Dict[str, Any]:
    """Strip _id and (when not full) replace huge prompt/response
    with previews."""
    doc.pop("_id", None)
    if not full:
        doc["prompt"] = _preview(doc.get("prompt"))
        doc["response"] = _preview(doc.get("response"))
    return doc


# ───────────────────────── GET /ledger ────────────────────────────────


@router.get("/ledger")
async def list_ledger(
    hours: int = Query(default=24, ge=1, le=168),
    limit: int = Query(default=50, ge=1, le=200),
    role: Optional[str] = Query(default=None),
    provider: Optional[str] = Query(default=None),
    only_ungraded: bool = Query(default=False),
    _user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """Recent LLM calls (preview-only)."""
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    q: Dict[str, Any] = {"created_at": {"$gte": since.isoformat()}}
    if role:
        q["role"] = role
    if provider:
        q["provider"] = provider

    cursor = (
        db[LLM_CALLS]
        .find(q)
        .sort("created_at", -1)
        .limit(limit)
    )
    items: List[Dict[str, Any]] = []
    async for doc in cursor:
        items.append(_serialize_row(doc, full=False))

    # Attach grade count per row in a single $in lookup.
    call_ids = [it["call_id"] for it in items if it.get("call_id")]
    grades_by_id: Dict[str, List[Dict[str, Any]]] = {cid: [] for cid in call_ids}
    if call_ids:
        async for g in db[LLM_PREFERENCE_LOG].find(
            {"call_id": {"$in": call_ids}}, {"_id": 0},
        ):
            cid = g.get("call_id")
            if cid in grades_by_id:
                grades_by_id[cid].append(g)
    for it in items:
        cid = it.get("call_id")
        glist = grades_by_id.get(cid, []) if cid else []
        latest = max(glist, key=lambda g: g.get("created_at", ""), default=None)
        it["grades_count"] = len(glist)
        it["latest_grade"] = latest

    if only_ungraded:
        items = [it for it in items if it["grades_count"] == 0]

    return {
        "ok": True,
        "since": since.isoformat(),
        "count": len(items),
        "items": items,
    }


# ───────────────────────── GET /ledger/{call_id} ──────────────────────


@router.get("/ledger/{call_id}")
async def get_ledger_row(
    call_id: str = Path(..., description="call_id from /ledger"),
    _user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """Full prompt + response + all grades for a single call."""
    doc = await db[LLM_CALLS].find_one({"call_id": call_id})
    if not doc:
        raise HTTPException(status_code=404, detail=f"call_id {call_id!r} not found")
    grades: List[Dict[str, Any]] = []
    async for g in db[LLM_PREFERENCE_LOG].find(
        {"call_id": call_id}, {"_id": 0},
    ).sort("created_at", -1):
        grades.append(g)
    return {
        "ok": True,
        "call": _serialize_row(doc, full=True),
        "grades": grades,
    }


# ───────────────────────── POST /ledger/{call_id}/grade ───────────────


class GradeRequest(BaseModel):
    score: int = Field(..., description="Score in [-2..2]. UI uses {-1,0,1}.")
    outcome: str = Field(..., min_length=1, max_length=64,
                         description="Short outcome tag (e.g. 'helpful', 'wrong')")
    note: Optional[str] = Field(default=None, max_length=1000)


@router.post("/ledger/{call_id}/grade")
async def grade_ledger_row(
    body: GradeRequest,
    call_id: str = Path(...),
    user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """Operator grade on an LLM call. Routes into the training
    pipeline ONLY — does NOT affect execution or promotion."""
    if body.score not in VALID_SCORES:
        raise HTTPException(
            status_code=400,
            detail=f"score must be in {sorted(VALID_SCORES)}; got {body.score}",
        )
    doc = await db[LLM_CALLS].find_one({"call_id": call_id}, {"call_id": 1})
    if not doc:
        raise HTTPException(status_code=404, detail=f"call_id {call_id!r} not found")

    grader = user.get("email", "operator")
    pref = await record_preference(
        call_id=call_id,
        score=body.score,
        outcome=body.outcome,
        note=body.note,
        grader=grader,
    )

    # Auto-enqueue winners into distillation queue (idempotent).
    enqueued = None
    if body.score >= MIN_ENQUEUE_SCORE:
        enqueued = await enqueue_training_pair(
            call_id=call_id,
            score=body.score,
            outcome=body.outcome,
            note=body.note,
        )

    return {
        "ok": True,
        "preference": pref,
        "enqueued_for_distillation": enqueued is not None,
    }
