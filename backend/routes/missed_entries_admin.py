"""Missed-Entry Ledger admin (2026-08-04) — counterfactual outcomes
for blocked BUYs. Observe-only evidence for gate tuning."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from auth import get_current_user
from db import db

router = APIRouter(prefix="/admin/missed-entries", tags=["missed-entries"])


@router.get("")
async def missed_entries_get(
    hours: int = 168,
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    from shared.risk_sizer.missed_entries import get_config, ledger_stats  # noqa: WPS433
    hours = max(1, min(int(hours), 720))
    return {"ok": True, "config": await get_config(), "hours": hours,
            **await ledger_stats(db, hours)}


class LedgerKnobs(BaseModel):
    enabled: Optional[bool] = None
    horizon_h: Optional[float] = Field(default=None, ge=0.5, le=24)


@router.post("")
async def missed_entries_update(
    body: LedgerKnobs,
    user: dict = Depends(get_current_user),  # noqa: B008
):
    from datetime import datetime, timezone  # noqa: WPS433
    from shared.risk_sizer.missed_entries import FLAG_ID, get_config  # noqa: WPS433
    changes = {k: v for k, v in body.model_dump().items() if v is not None}
    if changes:
        changes["updated_at"] = datetime.now(timezone.utc).isoformat()
        changes["updated_by"] = user.get("email") or "operator"
        await db["runtime_flags"].update_one(
            {"_id": FLAG_ID}, {"$set": changes}, upsert=True)
    return {"ok": True, "config": await get_config()}


@router.post("/run-once")
async def missed_entries_run_once(
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    from shared.risk_sizer.missed_entries import run_cycle  # noqa: WPS433
    return {"ok": True, "stats": await run_cycle()}
