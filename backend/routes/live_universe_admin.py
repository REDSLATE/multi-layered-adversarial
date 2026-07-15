"""Live Universe admin surface.

Read-only diagnostic view of the ephemeral universe built by
`shared/universe/refresher.py`. Complements the Symbol Registry
admin route — where the registry answers "does the broker know
this symbol?", this route answers "what's actually in the
universe right now?".

Endpoints:
    GET /api/admin/universe/live?lane=equity      — current universe
    GET /api/admin/universe/refresh-reports       — recent refresh audit
    POST /api/admin/universe/refresh              — trigger an immediate
                                                    refresh (manual)
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from auth import get_current_user
from shared.universe.live_universe import (
    KNOWN_LANES,
    read_all_universes,
    read_universe,
    recent_refresh_reports,
)
from shared.universe.refresher import refresh_all_lanes


logger = logging.getLogger("risedual.universe_admin")
router = APIRouter(prefix="/admin/universe", tags=["universe"])


@router.get("/live")
async def get_live_universe(
    lane: Optional[str] = Query(None, description="Filter to one lane (equity|crypto)"),
    _user=Depends(get_current_user),
) -> dict:
    """Return the active universe doc(s). Every doc carries
    generation_id + symbols[] with per-symbol source_reasons so
    the operator can see WHY a symbol is in the universe."""
    if lane:
        lane_u = lane.lower().strip()
        if lane_u not in KNOWN_LANES:
            raise HTTPException(400, f"unknown lane {lane!r}")
        doc = await read_universe(lane_u)
        return {"ok": True, "lane": lane_u, "universe": doc}
    docs = await read_all_universes()
    return {
        "ok": True,
        "lanes": list(docs.keys()),
        "universes": docs,
    }


@router.get("/refresh-reports")
async def get_refresh_reports(
    lane: Optional[str] = Query(None),
    limit: int = Query(20, ge=1, le=200),
    _user=Depends(get_current_user),
) -> dict:
    """Newest-first refresh audit. Each row lists added / retained /
    removed / quarantined symbols so the operator can see universe
    churn across time."""
    reports = await recent_refresh_reports(lane=lane, limit=limit)
    return {"ok": True, "count": len(reports), "reports": reports}


@router.post("/refresh")
async def trigger_refresh(_user=Depends(get_current_user)) -> dict:
    """Force an immediate refresh of both lanes. Bypasses the
    equity market-window guard so the operator can verify the
    fetcher works at any time of day."""
    result = await refresh_all_lanes(force=True)
    return {"ok": True, "result": result}
