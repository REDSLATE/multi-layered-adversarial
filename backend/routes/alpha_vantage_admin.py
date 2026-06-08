"""Alpha Vantage operator endpoints.

Read-only surface for the operator dashboard:
    GET  /api/admin/alpha-vantage/quota        — today's quota state
    GET  /api/admin/alpha-vantage/cache        — recent cached rows
    POST /api/admin/alpha-vantage/fetch        — fetch via the cached
                                                 feeder (operator probe)

Doctrine pin: these are READ + manual-probe routes only. The feeder
itself is the consumer-facing API (`shared.feeders.alpha_vantage.get_payload`).
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from auth import get_current_user
from db import db
from namespaces import ALPHA_VANTAGE_CACHE
from shared.feeders.alpha_vantage import get_payload, quota_state


router = APIRouter(prefix="/admin/alpha-vantage", tags=["admin", "alpha-vantage"])


class FetchBody(BaseModel):
    symbol: str = Field(..., min_length=1, max_length=32)
    function: str = Field(..., min_length=1, max_length=64)
    force_refresh: bool = False
    extra_params: Optional[dict] = None


@router.get("/quota")
async def av_quota(
    date: Optional[str] = Query(default=None, description="YYYY-MM-DD UTC; default today"),
    _user: dict = Depends(get_current_user),
):
    """Today's quota state (or a specific day's)."""
    return await quota_state(date)


@router.get("/cache")
async def av_cache_list(
    symbol: Optional[str] = Query(default=None),
    function: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    _user: dict = Depends(get_current_user),
):
    """Recent cached rows, newest first. Operator inspection only."""
    q: dict = {}
    if symbol:
        q["symbol"] = symbol.upper()
    if function:
        q["function"] = function.upper()
    rows = await db[ALPHA_VANTAGE_CACHE].find(
        q,
        {"_id": 0, "symbol": 1, "function": 1, "date": 1, "fetched_at": 1},
    ).sort("fetched_at", -1).to_list(limit)
    return {"items": rows, "count": len(rows)}


@router.post("/fetch")
async def av_fetch(
    body: FetchBody,
    _user: dict = Depends(get_current_user),
):
    """Manual operator probe — runs through the same cache+quota path
    every consumer uses, so we can verify the cache hit/miss behavior
    end-to-end from the dashboard."""
    result = await get_payload(
        body.symbol.upper(),
        body.function.upper(),
        extra_params=body.extra_params,
        force_refresh=body.force_refresh,
    )
    if not result["ok"]:
        # Surface the soft error as 200 with `ok=false` so operator
        # UIs can render the failure state without try/catch flow.
        # The error field carries the canonical reason
        # (quota_exhausted / upstream_error / no_api_key).
        if result["error"] == "quota_exhausted":
            # Still 200 — quota_exhausted is expected, not an error.
            return result
        if result["error"] == "no_api_key":
            raise HTTPException(
                status_code=503,
                detail=result["message"],
            )
        # upstream_error
        raise HTTPException(status_code=502, detail=result["message"])
    return result
