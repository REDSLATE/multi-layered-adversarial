"""Advisor performance admin route — operator-pinned table answering
"who's the best advisor and who's just noisy?" over multi-day windows.

Read-only. Joins `intent_consensus_telemetry` (executor decisions)
with `shared_brain_outcomes` (win/loss labels by intent_id).
"""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends, Query

from auth import get_current_user
from db import db
from shared.advisor_performance import advisor_performance


router = APIRouter(prefix="/admin/advisor-performance", tags=["admin-advisor-performance"])


@router.get("")
async def advisor_performance_endpoint(
    hours: int = Query(default=168, ge=1, le=720),
    _user: dict = Depends(get_current_user),  # noqa: B008
) -> Dict[str, Any]:
    """Per-advisor performance over the last `hours`.
    Default 168h (7 days) — matches the telemetry TTL.
    Max 720h (30d) for ad-hoc longer-range analysis once telemetry
    accumulates that far.
    """
    return await advisor_performance(db, hours)
