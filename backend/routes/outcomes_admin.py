"""Signal Outcome + Attribution admin (2026-08 operator directive).

GET  /api/admin/outcomes/rollup   — per-brain / per-attribution aggregates
GET  /api/admin/outcomes/recent   — latest outcome records
POST /api/admin/outcomes/resolve  — force one collector cycle now
GET  /api/admin/outcomes/status   — collector loop health
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query

from auth import get_current_user

router = APIRouter(prefix="/admin/outcomes", tags=["outcomes"])


@router.get("/rollup")
async def outcomes_rollup(
    since: Optional[str] = None,
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    from shared.outcome_engine import store  # noqa: WPS433
    return {"ok": True, **store.rollup(since)}


@router.get("/recent")
async def outcomes_recent(
    limit: int = Query(50, ge=1, le=500),
    brain: Optional[str] = None,
    lane: Optional[str] = None,
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    from shared.outcome_engine import store  # noqa: WPS433
    return {"ok": True, "rows": store.recent(limit, brain=brain, lane=lane)}


@router.post("/resolve")
async def outcomes_resolve(
    limit: int = Query(50, ge=1, le=500),
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    from shared.outcome_engine.collector import resolve_batch  # noqa: WPS433
    return {"ok": True, **await resolve_batch(limit)}


@router.get("/status")
async def outcomes_status(_user: dict = Depends(get_current_user)):  # noqa: B008
    from shared.outcome_engine.collector import get_status_async  # noqa: WPS433
    return {"ok": True, **await get_status_async()}
