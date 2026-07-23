"""Expectancy Panel admin — fee/spread-adjusted realized performance."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from auth import get_current_user
from shared.expectancy import LANE_FIELDS, get_config, set_config, summary

router = APIRouter(prefix="/admin/expectancy", tags=["expectancy"])


@router.get("")
async def expectancy_overview(
    days: int = 30,
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    days = max(1, min(365, int(days)))
    return await summary(days)


@router.get("/config")
async def expectancy_config(_user: dict = Depends(get_current_user)):  # noqa: B008
    return {"config": await get_config()}


@router.post("/config")
async def update_expectancy_config(
    body: dict,
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    lane = (body.get("lane") or "").strip().lower()
    if lane not in ("equity", "crypto"):
        raise HTTPException(status_code=422, detail="lane must be equity|crypto")
    fields: dict = {}
    for k, lo, hi in (("taker_fee_pct", 0.0, 5.0), ("spread_bps", 0.0, 500.0)):
        if k in body and body[k] is not None:
            try:
                v = float(body[k])
            except (TypeError, ValueError):
                raise HTTPException(status_code=422, detail=f"{k} must be a number")
            if not (lo <= v <= hi):
                raise HTTPException(
                    status_code=422, detail=f"{k} out of range [{lo}, {hi}]",
                )
            fields[k] = v
    if not fields:
        raise HTTPException(status_code=422, detail="nothing to update")
    assert set(fields) <= LANE_FIELDS
    cfg = await set_config(lane, fields, _user.get("email") or "unknown")
    return {"ok": True, "config": cfg}
