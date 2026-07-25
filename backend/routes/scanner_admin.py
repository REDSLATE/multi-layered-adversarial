"""RTH Opportunity Scanner admin — status, pool, manual scan, policy.
Scanner is ADVISORY only; nothing here can place or size trades."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from auth import get_current_user
from db import db
from shared.scanner import store
from shared.scanner.rth_scanner import DEFAULT_POLICY, get_policy, get_status, scan_once
from shared.scanner.universe500 import discovery_universe

router = APIRouter(prefix="/admin/scanner", tags=["scanner"])


@router.get("")
async def scanner_overview(_user: dict = Depends(get_current_user)):  # noqa: B008
    policy = await get_policy()
    return {
        "status": get_status(),
        "policy": policy,
        "universe_size": len(discovery_universe(policy)),
        "candidates": store.live_candidates(limit=25),
    }


@router.post("/scan-now")
async def scanner_scan_now(_user: dict = Depends(get_current_user)):  # noqa: B008
    return {"ok": True, "result": await scan_once(force=True)}


@router.post("/policy")
async def scanner_policy(body: dict, _user: dict = Depends(get_current_user)):  # noqa: B008
    update: dict = {}
    for k, lo, hi in (
        ("min_price", 0.0, 1000.0),
        ("min_hourly_dollar_vol", 0.0, 1e10),
        ("min_bars", 5, 100),
        ("max_bar_age_min", 1.0, 240.0),
    ):
        if k in body and body[k] is not None:
            try:
                v = float(body[k])
            except (TypeError, ValueError):
                raise HTTPException(status_code=422, detail=f"{k} must be a number")
            if not (lo <= v <= hi):
                raise HTTPException(status_code=422, detail=f"{k} out of range")
            update[k] = v
    if "allow_leveraged" in body:
        update["allow_leveraged"] = bool(body["allow_leveraged"])
    for k in ("extra_symbols", "exclude_symbols"):
        if k in body:
            v = body[k]
            if v is not None and not isinstance(v, list):
                raise HTTPException(status_code=422, detail=f"{k} must be a list")
            update[k] = (
                [str(s).upper().strip() for s in v[:200]] if v else []
            )
    if not update:
        raise HTTPException(status_code=422, detail="nothing to update")
    assert set(update) <= set(DEFAULT_POLICY)
    await db["runtime_flags"].update_one(
        {"_id": "scanner_policy"}, {"$set": update}, upsert=True,
    )
    return {"ok": True, "policy": await get_policy()}
