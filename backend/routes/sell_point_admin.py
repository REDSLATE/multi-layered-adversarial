"""Sell-Point Watcher admin (2026-08-04, v3.5 plan item 5)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, Literal, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from auth import get_current_user
from db import db

router = APIRouter(prefix="/admin/sell-point", tags=["sell-point"])


@router.get("")
async def sell_point_get(_user: dict = Depends(get_current_user)):  # noqa: B008
    from shared.exits.pattern_watch import EVENTS, get_config  # noqa: WPS433
    events = await db[EVENTS].find({}, {}).sort(
        "created_at", -1).max_time_ms(5000).to_list(20)
    return {"ok": True, "config": await get_config(), "events": events}


class SellPointKnobs(BaseModel):
    enabled: Optional[bool] = None
    mode: Optional[Literal["observe", "act"]] = None
    interval_sec: Optional[int] = Field(default=None, ge=30, le=3600)
    cooldown_min: Optional[float] = Field(default=None, ge=5, le=1440)
    stop_buffer_atr: Optional[float] = Field(default=None, ge=0.1, le=5)
    actions: Optional[Dict[str, Literal["off", "tighten", "exit"]]] = None


@router.post("")
async def sell_point_update(
    body: SellPointKnobs,
    user: dict = Depends(get_current_user),  # noqa: B008
):
    from shared.exits.pattern_watch import FLAG_ID, PATTERNS, get_config  # noqa: WPS433
    changes = {k: v for k, v in body.model_dump().items() if v is not None}
    if "actions" in changes:
        current = (await get_config())["actions"]
        merged = {**current, **{k: v for k, v in changes["actions"].items()
                                if k in PATTERNS}}
        changes["actions"] = merged
    if changes:
        changes["updated_at"] = datetime.now(timezone.utc).isoformat()
        changes["updated_by"] = user.get("email") or "operator"
        await db["runtime_flags"].update_one(
            {"_id": FLAG_ID}, {"$set": changes}, upsert=True)
    return {"ok": True, "config": await get_config()}


@router.post("/run-once")
async def sell_point_run_once(
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    from shared.exits.pattern_watch import run_once  # noqa: WPS433
    return {"ok": True, "stats": await run_once()}
