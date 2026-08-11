"""Execution Recovery Ladder admin (2026-06 operator directive).

Runtime knob panel — ladder stage timing, chase cap and spread
thresholds are adjustable live (runtime_flags) so tuning never needs a
redeploy. Also exposes live hunt activity so the operator can watch
the ladder work in real time.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from auth import get_current_user
from db import db

router = APIRouter(prefix="/admin/execution-ladder", tags=["execution-ladder"])

LADDER_FLAG = "execution_ladder"
ELIG_FLAG = "buy_eligibility"
ACTIVE = "execution_ladder_active"
EVENTS = "execution_ladder_events"


class LadderConfigUpdate(BaseModel):
    enabled: Optional[bool] = None
    stage_wait_s: Optional[float] = Field(None, ge=2.0, le=60.0)
    poll_s: Optional[float] = Field(None, ge=0.5, le=10.0)
    adaptive_spread_frac: Optional[float] = Field(None, ge=0.0, le=1.0)
    max_chase_bps: Optional[float] = Field(None, ge=0.0, le=1000.0)
    max_spread_bps: Optional[float] = Field(None, ge=5.0, le=500.0)
    hard_reject_spread_bps: Optional[float] = Field(None, ge=50.0, le=2000.0)


async def _merged_config() -> dict:
    from shared.execution_ladder import get_ladder_config  # noqa: WPS433
    from shared.risk_sizer.buy_eligibility import (  # noqa: WPS433
        get_eligibility_config,
    )
    ladder = await get_ladder_config()
    elig = await get_eligibility_config()
    return {
        "ladder": {k: ladder.get(k) for k in
                   ("enabled", "stage_wait_s", "poll_s",
                    "adaptive_spread_frac", "max_chase_bps")},
        "spread": {
            "max_spread_bps": elig.get("max_spread_bps"),
            "hard_reject_spread_bps": elig.get("hard_reject_spread_bps"),
        },
    }


@router.get("/config")
async def ladder_config_get(_user: dict = Depends(get_current_user)):  # noqa: B008
    return {"ok": True, **await _merged_config()}


@router.post("/config")
async def ladder_config_set(
    body: LadderConfigUpdate,
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    ladder_keys = {"enabled", "stage_wait_s", "poll_s",
                   "adaptive_spread_frac", "max_chase_bps"}
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(400, "no knobs provided")
    spread_updates = {k: v for k, v in updates.items() if k not in ladder_keys}
    ladder_updates = {k: v for k, v in updates.items() if k in ladder_keys}
    if spread_updates:
        from shared.risk_sizer.buy_eligibility import (  # noqa: WPS433
            get_eligibility_config,
        )
        cur = await get_eligibility_config()
        max_s = spread_updates.get("max_spread_bps", cur.get("max_spread_bps"))
        hard_s = spread_updates.get(
            "hard_reject_spread_bps", cur.get("hard_reject_spread_bps"))
        if float(hard_s) <= float(max_s):
            raise HTTPException(
                400, "hard_reject_spread_bps must exceed max_spread_bps")
    stamp = {"updated_at": datetime.now(timezone.utc).isoformat(),
             "updated_by": _user.get("email")}
    if ladder_updates:
        await db["runtime_flags"].update_one(
            {"_id": LADDER_FLAG}, {"$set": {**ladder_updates, **stamp}},
            upsert=True)
    if spread_updates:
        await db["runtime_flags"].update_one(
            {"_id": ELIG_FLAG}, {"$set": {**spread_updates, **stamp}},
            upsert=True)
        from shared.risk_sizer import buy_eligibility as elig  # noqa: WPS433
        elig.reset_for_tests()  # bust the 30s config cache immediately
    return {"ok": True, "applied": updates, **await _merged_config()}


@router.get("/activity")
async def ladder_activity(_user: dict = Depends(get_current_user)):  # noqa: B008
    """Live hunts (updated within the last 3 min) + last 10 terminal
    events — polled by the UI for real-time hunt toasts."""
    stale = (datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat()
    active = await db[ACTIVE].find(
        {"updated_at": {"$gte": stale}}, {"_id": 0},
    ).sort("updated_at", -1).max_time_ms(5000).to_list(20)
    recent = await db[EVENTS].find(
        {}, {"_id": 0},
    ).sort("ts", -1).max_time_ms(5000).to_list(10)
    return {"ok": True, "active": active, "recent": recent}
