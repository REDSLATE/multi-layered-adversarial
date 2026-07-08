"""Admin route — feature-coverage report.

Read-only diagnostic. Answers: are the doctrine seats getting the
inputs they need?
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from auth import get_current_user
from shared.coverage_report import build_coverage_report


router = APIRouter(tags=["admin"])


@router.get("/admin/feature-coverage-report")
async def feature_coverage_report(
    scope: str = Query(
        default="live_universe",
        description=(
            "'live_universe' — symbols actively traded in the last 24h. "
            "'all_snapshots' — every symbol with a cached snapshot."
        ),
    ),
    _user=Depends(get_current_user),
):
    """Coverage per doctrine-facing field + feeder health per source.

    Response includes `missing_symbols` samples per field so the
    operator can drill into WHICH symbols are dark without having
    to run follow-up queries. Read-only — never mutates.
    """
    if scope not in {"live_universe", "all_snapshots"}:
        raise HTTPException(
            status_code=400,
            detail=f"scope must be 'live_universe' or 'all_snapshots', got {scope!r}",
        )
    return await build_coverage_report(scope)
