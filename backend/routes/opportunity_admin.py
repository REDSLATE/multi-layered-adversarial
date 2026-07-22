"""Opportunity policy admin — aggression knobs + kernel visibility."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException

from auth import get_current_user
from db import db
from shared.brains.kernel_throttle import (
    get_kernel_throttle, invalidate_kernel_cache,
)
from shared.opportunity.policy import (
    POLICY_FLAG_ID, get_opportunity_policy, invalidate_policy_cache,
)

router = APIRouter(prefix="/admin/opportunity-policy", tags=["opportunity-policy"])

BRAINS = ("gto", "camino", "barracuda", "hellcat")


@router.get("")
async def policy_overview(_user: dict = Depends(get_current_user)):  # noqa: B008
    kernel_view = {}
    for lane in ("equity", "crypto"):
        kernel_view[lane] = {
            b: await get_kernel_throttle(b, lane) for b in BRAINS
        }
    return {
        "policy": await get_opportunity_policy(),
        "kernel_throttle": kernel_view,
    }


def _fnum(v, lo: float, hi: float, name: str) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail=f"{name} must be a number")
    if not (lo <= f <= hi):
        raise HTTPException(status_code=422, detail=f"{name} out of range [{lo}, {hi}]")
    return f


@router.post("")
async def update_policy(
    body: dict,
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    update: dict = {}
    if "tiers_enabled" in body:
        update["tiers_enabled"] = bool(body["tiers_enabled"])
    am = body.get("authority_min") or {}
    for lane in ("equity", "crypto"):
        if lane in am and am[lane] is not None:
            update[f"authority_min.{lane}"] = _fnum(
                am[lane], 1.0, 1440.0, f"authority_min.{lane}",
            )
    tiers = body.get("tiers") or {}
    for lane in ("equity", "crypto"):
        lt = tiers.get(lane) or {}
        vals = {}
        for k in ("probe", "enter", "press"):
            if k in lt and lt[k] is not None:
                vals[k] = _fnum(lt[k], 0.0, 1.0, f"tiers.{lane}.{k}")
        merged = {**(await get_opportunity_policy())["tiers"][lane], **vals}
        if not (merged["probe"] <= merged["enter"] <= merged["press"]):
            raise HTTPException(
                status_code=422,
                detail=f"tiers.{lane} must satisfy probe ≤ enter ≤ press",
            )
        for k, v in vals.items():
            update[f"tiers.{lane}.{k}"] = v
    tn = body.get("tier_notionals") or {}
    for k in ("probe", "enter", "full"):
        if k in tn and tn[k] is not None:
            update[f"tier_notionals.{k}"] = _fnum(
                tn[k], 1.0, 10_000.0, f"tier_notionals.{k}",
            )
    kn = body.get("kernel") or {}
    if "enabled" in kn:
        update["kernel.enabled"] = bool(kn["enabled"])
    for k, lo, hi in (("min_mult", 0.1, 1.0), ("max_mult", 1.0, 3.0)):
        if k in kn and kn[k] is not None:
            update[f"kernel.{k}"] = _fnum(kn[k], lo, hi, f"kernel.{k}")

    if not update:
        raise HTTPException(status_code=422, detail="nothing to update")
    update["updated_by"] = _user.get("email") or "unknown"
    update["updated_at"] = datetime.now(timezone.utc).isoformat()
    await db["runtime_flags"].update_one(
        {"_id": POLICY_FLAG_ID}, {"$set": update}, upsert=True,
    )
    invalidate_policy_cache()
    invalidate_kernel_cache()
    return {"ok": True, "policy": await get_opportunity_policy()}
