"""Exit Monitor admin — policy knobs, plan visibility, CLOSE NOW."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from auth import get_current_user
from db import db
from shared.exits import monitor as exit_monitor
from shared.exits.policy import get_policy, set_policy

router = APIRouter(prefix="/admin/exits", tags=["exit-monitor"])


@router.get("")
async def exits_overview(_user: dict = Depends(get_current_user)):  # noqa: B008
    plans = []
    cursor = db[exit_monitor.EXIT_PLANS].find(
        {"status": {"$in": ["active", "exiting", "error"]}}, {"_id": 0},
    ).sort("adopted_at", -1).limit(100)
    async for p in cursor:
        plans.append(p)
    recent = []
    async for r in db[exit_monitor.EXIT_RECEIPTS].find(
        {}, {"_id": 0},
    ).sort("ts", -1).limit(20):
        recent.append(r)
    return {
        "policy": await get_policy(),
        "monitor": exit_monitor.get_status(),
        "plans": plans,
        "recent_receipts": recent,
    }


@router.post("/policy")
async def update_policy(
    body: dict,
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    lane = (body.get("lane") or "").strip().lower()
    if lane not in ("equity", "crypto"):
        raise HTTPException(status_code=422, detail="lane must be equity|crypto")
    fields: dict = {}
    if "enabled" in body:
        fields["enabled"] = bool(body["enabled"])
    for k, lo, hi in (
        ("sl_pct", 0.1, 50.0), ("tp_pct", 0.1, 100.0), ("max_hold_h", 0.5, 720.0),
    ):
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
    policy = await set_policy(lane, fields, _user.get("email") or "unknown")
    return {"ok": True, "policy": policy}


@router.post("/close-now")
async def close_now(
    body: dict,
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    plan_id = (body.get("plan_id") or "").strip()
    if not plan_id:
        raise HTTPException(status_code=422, detail="plan_id required")
    result = await exit_monitor.close_now(plan_id)
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("error") or "close failed")
    return result


@router.post("/run-once")
async def run_once(_user: dict = Depends(get_current_user)):  # noqa: B008
    return {"ok": True, "summary": await exit_monitor.run_once()}
