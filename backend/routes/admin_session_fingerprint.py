"""Session fingerprint read endpoints.

Read-only. Fingerprints are computed by the background worker in
`shared/session_fingerprint.py`. This module surfaces them to the
Ops Room for before/after doctrine-change validation.
"""
from __future__ import annotations

from typing import Any, Dict, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from auth import get_current_user
from db import db
from namespaces import SESSION_FINGERPRINTS
from shared.session_fingerprint import BRAINS, LANES, run_now


router = APIRouter(
    prefix="/admin/fingerprints", tags=["admin", "fingerprints"],
)


@router.get("/latest")
async def get_latest_fingerprints(
    brain: Optional[str] = None,
    lane: Optional[str] = None,
    limit: int = Query(20, ge=1, le=200),
    _user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """Return the most recent fingerprints, newest first.

    Filters:
        brain — optional, must be one of the 4 canonical brains
        lane  — optional, `equity` or `crypto`
        limit — max rows, 1..200
    """
    query: Dict[str, Any] = {}
    if brain:
        if brain not in BRAINS:
            raise HTTPException(400, detail=f"invalid brain {brain!r}")
        query["brain"] = brain
    if lane:
        if lane not in LANES:
            raise HTTPException(400, detail=f"invalid lane {lane!r}")
        query["lane"] = lane

    rows = await db[SESSION_FINGERPRINTS].find(
        query, sort=[("window_end_ts", -1)],
    ).limit(limit).to_list(limit)
    return {"count": len(rows), "fingerprints": rows}


@router.get("/window/{brain}/{lane}")
async def get_window_fingerprint(
    brain: Literal["camino", "barracuda", "hellcat", "gto"],
    lane: Literal["equity", "crypto"],
    window_end_ts: str = Query(
        ..., description="Exact `window_end_ts` value from a prior fingerprint",
    ),
    _user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """Pull one specific fingerprint by (brain, lane, window_end_ts)."""
    doc = await db[SESSION_FINGERPRINTS].find_one({
        "brain": brain, "lane": lane, "window_end_ts": window_end_ts,
    })
    if not doc:
        raise HTTPException(
            404, detail=f"no fingerprint for {brain}/{lane}@{window_end_ts}",
        )
    return doc


@router.post("/run-now")
async def trigger_fingerprint_now(
    _user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """Manual re-trigger of the current-window fingerprint pass.
    Useful right after a doctrine change to snapshot immediately
    without waiting for the next scheduled tick."""
    return await run_now()
