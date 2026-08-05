"""Entry-mode + promotion-gate admin (2026-08-05 operator directive).

Mode changes to canary/live are REFUSED while the promotion gate is
red unless the operator sends override=true — deployment is earned on
forward-recorded expectancy, not on passing tests.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from auth import get_current_user
from db import db

router = APIRouter(prefix="/admin/entry-mode", tags=["entry-mode"])


@router.get("")
async def entry_mode_get(_user: dict = Depends(get_current_user)):  # noqa: B008
    from shared.execution_mode import (  # noqa: WPS433
        SHADOW_FILLS, get_entry_mode_config,
    )
    shadows = await db[SHADOW_FILLS].find({}, {"_id": 0}).sort(
        "ts", -1).max_time_ms(5000).to_list(10)
    n_shadow = await db[SHADOW_FILLS].count_documents({}, maxTimeMS=5000)
    return {"ok": True, "config": await get_entry_mode_config(),
            "shadow_fills_total": n_shadow, "recent_shadow_fills": shadows}


class ModeBody(BaseModel):
    mode: Optional[Literal["live", "exit_only", "canary"]] = None
    canary_max_trades_per_day: Optional[int] = Field(
        default=None, ge=1, le=50)
    override: bool = False


@router.post("")
async def entry_mode_update(
    body: ModeBody,
    user: dict = Depends(get_current_user),  # noqa: B008
):
    from shared.execution_mode import FLAG_ID, get_entry_mode_config  # noqa: WPS433
    changes = {k: v for k, v in body.model_dump().items()
               if v is not None and k != "override"}
    if body.mode in ("live", "canary") and not body.override:
        from shared.forensics.promotion_gate import gate_status  # noqa: WPS433
        gate = await gate_status()
        if not gate["passed"]:
            raise HTTPException(409, {
                "error": "promotion_gate_not_met",
                "detail": ("automated entries return only after "
                           "forward-recorded expectancy is positive; "
                           "send override=true to force (audited)"),
                "gate": {k: gate[k] for k in ("per_lane", "passed_lanes")},
            })
    if changes:
        changes["updated_at"] = datetime.now(timezone.utc).isoformat()
        changes["updated_by"] = user.get("email") or "operator"
        if body.override:
            changes["override_used"] = True
        await db["runtime_flags"].update_one(
            {"_id": FLAG_ID}, {"$set": changes}, upsert=True)
    return {"ok": True, "config": await get_entry_mode_config()}


@router.get("/promotion-gate")
async def promotion_gate_get(_user: dict = Depends(get_current_user)):  # noqa: B008
    from shared.forensics.promotion_gate import gate_status  # noqa: WPS433
    return {"ok": True, **await gate_status()}
