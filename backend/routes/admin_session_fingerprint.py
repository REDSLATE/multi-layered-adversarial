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
from shared.session_fingerprint import BRAINS, LANES, diff_fingerprints, run_now


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


@router.get("/diff")
async def get_fingerprint_diff(
    brain: Literal["camino", "barracuda", "hellcat", "gto"] = Query(...),
    lane: Literal["equity", "crypto"] = Query(...),
    before_start_ts: str = Query(
        ..., description="ISO8601 UTC — start of the BEFORE window (inclusive)",
    ),
    before_end_ts: str = Query(
        ..., description="ISO8601 UTC — end of the BEFORE window (inclusive)",
    ),
    after_start_ts: str = Query(
        ..., description="ISO8601 UTC — start of the AFTER window (inclusive)",
    ),
    after_end_ts: str = Query(
        ..., description="ISO8601 UTC — end of the AFTER window (inclusive)",
    ),
    top_k: int = Query(5, ge=1, le=20),
    _user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """Diff two ranges of already-computed fingerprints for a
    (brain, lane) pair.

    Use case: operator changed a doctrine threshold at time T.
    Diff `[T-2h, T]` against `[T, T+2h]` to see whether the funnel
    shifted as expected — did execution_ready_rate rise? Did the
    top_fail_reason for a specific gate drop? Did quality_dist
    reweight toward A/B?

    Windows are inclusive on both ends. Percentile diffs are
    approximate (weighted-mean composite); count-based fields
    (intent_count, dist counts, top-K counts) are exact sums.

    Empty windows: if the fingerprint collection has no coverage
    for the requested range, `intent_count` will be 0 and deltas
    will surface that honestly. `windows_used` reports coverage
    so the operator knows if the diff is meaningful.
    """
    try:
        return await diff_fingerprints(
            brain=brain, lane=lane,
            before_start_ts=before_start_ts,
            before_end_ts=before_end_ts,
            after_start_ts=after_start_ts,
            after_end_ts=after_end_ts,
            top_k=top_k,
        )
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc)) from exc
