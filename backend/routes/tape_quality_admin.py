"""Tape Quality knobs admin (2026-08-04 operator request)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from auth import get_current_user
from db import db

router = APIRouter(prefix="/admin/tape-quality", tags=["tape-quality"])


@router.get("")
async def tape_quality_get(_user: dict = Depends(get_current_user)):  # noqa: B008
    from shared.market_data.tape_quality import get_tape_config  # noqa: WPS433
    return {"ok": True, "config": await get_tape_config()}


class TapeKnobs(BaseModel):
    enabled: Optional[bool] = None
    min_bars: Optional[int] = Field(default=None, ge=3, le=60)
    min_completeness: Optional[float] = Field(default=None, ge=0.5, le=1.0)
    max_gap_bars: Optional[float] = Field(default=None, ge=1, le=120)
    max_staleness_tf_mult: Optional[float] = Field(default=None, ge=1, le=60)


@router.post("")
async def tape_quality_update(
    body: TapeKnobs,
    user: dict = Depends(get_current_user),  # noqa: B008
):
    from shared.market_data.tape_quality import FLAG_ID, get_tape_config  # noqa: WPS433
    changes = {k: v for k, v in body.model_dump().items() if v is not None}
    if changes:
        changes["updated_at"] = datetime.now(timezone.utc).isoformat()
        changes["updated_by"] = user.get("email") or "operator"
        await db["runtime_flags"].update_one(
            {"_id": FLAG_ID}, {"$set": changes}, upsert=True)
    return {"ok": True, "config": await get_tape_config()}
