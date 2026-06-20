"""Lessons + Report Cards + Setup Memory HTTP surface.

All read-only or admin-only. No execution call paths.

Routes:
    GET  /api/lessons/{intent_id}         — labeled lesson for one intent
    GET  /api/lessons                     — filtered bulk
    GET  /api/report-cards/{brain}        — per-brain card
    GET  /api/report-cards/setup/{setup_id} — per-setup card (cross-brain)
    GET  /api/setup-memory/preview        — what WOULD the adjuster do
                                            for these args
    GET  /api/admin/setup-memory/status   — kill switch state + bucket table
    POST /api/admin/setup-memory/toggle   — flip the kill switch
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from auth import get_current_user
from db import db
from shared.lessons.builder import build_lesson, build_lessons_bulk
from shared.report_cards import build_report_card, build_setup_aggregate
from shared.setup_memory import (
    MIN_SAMPLE_SIZE,
    MULT_BOUND_MAX,
    MULT_BOUND_MIN,
    WINDOW_INTENTS,
    _BUCKETS,
    compute_adjustment,
    setup_memory_enabled,
)


router = APIRouter(tags=["verifier"])


# ──────────────────────── lessons ────────────────────────

@router.get("/lessons/{intent_id}")
async def get_lesson(
    intent_id: str,
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    lesson = await build_lesson(intent_id)
    if lesson is None:
        raise HTTPException(status_code=404, detail="intent not found")
    return lesson.__dict__


@router.get("/lessons")
async def list_lessons(
    stack: Optional[str] = Query(None),
    lane: Optional[str] = Query(None),
    symbol: Optional[str] = Query(None),
    setup_id: Optional[str] = Query(None),
    outcome: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=500),
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    rows = await build_lessons_bulk(
        stack=stack, lane=lane, symbol=symbol,
        setup_id=setup_id, outcome=outcome, limit=limit,
    )
    return {"count": len(rows), "lessons": [le.__dict__ for le in rows]}


# ──────────────────────── report cards ────────────────────────

@router.get("/report-cards/{brain}")
async def get_brain_report_card(
    brain: str,
    lane: Optional[str] = Query(None),
    setup_id: Optional[str] = Query(None),
    regime: Optional[str] = Query(None),
    limit: int = Query(500, ge=50, le=2000),
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    return await build_report_card(
        stack=brain, lane=lane, setup_id=setup_id, regime=regime, limit=limit,
    )


@router.get("/report-cards/setup/{setup_id:path}")
async def get_setup_report(
    setup_id: str,
    lane: Optional[str] = Query(None),
    limit: int = Query(1_000, ge=50, le=5000),
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    return await build_setup_aggregate(setup_id=setup_id, lane=lane, limit=limit)


# ──────────────────────── setup memory ────────────────────────

@router.get("/setup-memory/preview")
async def preview_setup_memory(
    brain: str = Query(...),
    lane: str = Query(...),
    action: str = Query(...),
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    """Dry-run the adjuster — returns the multiplier the gate would
    apply RIGHT NOW for (brain, lane, action). Useful for the
    operator to see "what would this trade get?" without actually
    submitting."""
    block = await compute_adjustment(
        stack=brain, lane=lane, action=action, research_signals=None,
    )
    enabled = await setup_memory_enabled()
    return {
        "would_apply": enabled,
        "kill_switch_state": "enabled" if enabled else "disabled",
        **block,
    }


@router.get("/admin/setup-memory/status")
async def setup_memory_status(_user: dict = Depends(get_current_user)):  # noqa: B008
    enabled = await setup_memory_enabled()
    return {
        "enabled": enabled,
        "bounds": {"min": MULT_BOUND_MIN, "max": MULT_BOUND_MAX},
        "min_sample_size": MIN_SAMPLE_SIZE,
        "window_intents": WINDOW_INTENTS,
        "buckets": [
            {"min_win_rate": thr, "multiplier": mult, "label": label}
            for thr, mult, label in _BUCKETS
        ],
    }


class ToggleBody(BaseModel):
    enabled: bool


@router.post("/admin/setup-memory/toggle")
async def toggle_setup_memory(
    body: ToggleBody,
    user: dict = Depends(get_current_user),  # noqa: B008
):
    """Flip the kill switch. Persists into `runtime_flags`. Audit
    trail: stamps `last_changed_by` + `last_changed_at`."""
    from datetime import datetime, timezone
    await db["runtime_flags"].update_one(
        {"key": "setup_memory_enabled"},
        {
            "$set": {
                "value": bool(body.enabled),
                "last_changed_by": user.get("email") or "?",
                "last_changed_at": datetime.now(timezone.utc).isoformat(),
            },
        },
        upsert=True,
    )
    return {"ok": True, "enabled": body.enabled}
