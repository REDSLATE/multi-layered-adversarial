"""Momentum scanner admin — status, knobs, arm/disarm (2026-08-01)."""
from __future__ import annotations

from typing import List, Literal, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from auth import get_current_user
from db import db

router = APIRouter(prefix="/admin/momentum-scanner",
                   tags=["momentum-scanner"])


class ScannerKnobs(BaseModel):
    enabled: Optional[bool] = None
    interval_sec: Optional[int] = Field(default=None, ge=15, le=900)
    cooldown_min: Optional[float] = Field(default=None, ge=1, le=1440)
    max_emit_per_cycle: Optional[int] = Field(default=None, ge=0, le=10)
    lanes: Optional[List[Literal["crypto", "equity"]]] = None
    tp_pct: Optional[float] = Field(default=None, ge=0.5, le=50)
    sl_pct: Optional[float] = Field(default=None, ge=0.5, le=50)
    min_score: Optional[float] = Field(default=None, ge=0, le=1)
    min_score_delta: Optional[float] = Field(default=None, ge=0, le=1)
    ignition_enabled: Optional[bool] = None
    ignition_top_n: Optional[int] = Field(default=None, ge=1, le=20)
    ignition_min_vol_usd_min: Optional[float] = Field(default=None, ge=100)


@router.get("")
async def scanner_status(_user: dict = Depends(get_current_user)):  # noqa: B008
    from momentum.momentum_scanner import FLAG_ID, STATE_ID, get_config  # noqa: WPS433
    cfg = await get_config()
    state = await db["runtime_flags"].find_one(
        {"_id": STATE_ID}, {"_id": 0}, max_time_ms=3000) or {}
    n_emitted = await db["shared_intents"].count_documents(
        {"stack": "momentum"}, maxTimeMS=5000)
    return {"ok": True, "config": cfg, "state": state,
            "total_momentum_intents": n_emitted, "flag_id": FLAG_ID}


@router.post("")
async def scanner_update(
    body: ScannerKnobs,
    user: dict = Depends(get_current_user),  # noqa: B008
):
    from datetime import datetime, timezone  # noqa: WPS433
    from momentum.momentum_scanner import FLAG_ID, get_config  # noqa: WPS433
    changes = {k: v for k, v in body.model_dump().items() if v is not None}
    if changes:
        changes["updated_at"] = datetime.now(timezone.utc).isoformat()
        changes["updated_by"] = user.get("email") or "operator"
        await db["runtime_flags"].update_one(
            {"_id": FLAG_ID}, {"$set": changes}, upsert=True)
    return {"ok": True, "config": await get_config()}
