"""Pipeline flow health — emission vs doctrine counters.

GET /api/admin/pipeline/counters
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from auth import get_current_user
from shared.observability.pipeline_counters import snapshot

router = APIRouter(prefix="/admin/pipeline", tags=["pipeline"])


@router.get("/counters")
async def pipeline_counters(_user: dict = Depends(get_current_user)):  # noqa: B008
    return {"ok": True, "counters": snapshot()}
