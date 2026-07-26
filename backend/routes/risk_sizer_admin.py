"""Risk Sizer admin — dynamic position sizing observability + knobs.

GET  /api/admin/risk-sizer            policy + live balance + open risk
POST /api/admin/risk-sizer/policy     merge-update policy knobs
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from db import db
from auth import get_current_user
from shared.risk_sizer import balance, open_risk
from shared.risk_sizer.policy import DEFAULTS, FLAG_ID, get_sizer_policy

router = APIRouter(prefix="/admin/risk-sizer", tags=["risk-sizer"])


class SizerPolicyBody(BaseModel):
    crypto: Optional[dict] = None
    equity: Optional[dict] = None
    options: Optional[dict] = None
    selection: Optional[dict] = None
    balance: Optional[dict] = None


@router.get("")
async def risk_sizer_status(_user: dict = Depends(get_current_user)):  # noqa: B008
    policy = await get_sizer_policy()
    out = {"policy": policy, "lanes": {}}
    for lane in ("crypto", "equity", "options"):
        lane_view = {
            "enabled": policy["enabled"][lane],
            "open_plan_risk_usd": round(open_risk.open_plan_risk(lane), 2),
            "pending_entry_risk_usd": round(open_risk.pending_risk(lane), 2),
        }
        if policy["enabled"][lane]:
            snap = await balance.get_balance_snapshot(lane)
            if snap:
                eq = snap["equity"]
                lp = policy[lane]
                lane_view.update({
                    "balance_source": snap["source"],
                    "balance_age_ms": snap["age_ms"],
                    "account_equity": round(eq, 2),
                    "available_quote_balance": round(snap["available"], 2),
                    "risk_budget_per_trade": round(eq * lp["risk_fraction"], 2),
                    "allocation_cap": round(eq * lp["max_position_fraction"], 2),
                    "max_open_risk": round(eq * lp["max_open_risk_fraction"], 2),
                    "remaining_risk_capacity": round(max(
                        0.0, eq * lp["max_open_risk_fraction"]
                        - open_risk.total_open_risk(lane)), 2),
                })
            else:
                lane_view["balance_source"] = "UNAVAILABLE"
        out["lanes"][lane] = lane_view
    return out


@router.post("/policy")
async def set_sizer_policy(
    body: SizerPolicyBody,
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    update: dict = {}
    for section in ("crypto", "equity", "options", "selection", "balance"):
        payload = getattr(body, section)
        if payload:
            bad = set(payload) - set(DEFAULTS[section])
            if bad:
                raise HTTPException(422, f"unknown {section} keys: {sorted(bad)}")
            for k, v in payload.items():
                update[f"{section}.{k}"] = v
    if not update:
        raise HTTPException(422, "no policy fields provided")
    update["updated_by"] = _user.get("email") or "unknown"
    await db["runtime_flags"].update_one(
        {"_id": FLAG_ID}, {"$set": update}, upsert=True,
    )
    from shared.risk_sizer.policy import invalidate_policy_cache
    invalidate_policy_cache()
    return {"ok": True, "policy": await get_sizer_policy()}
