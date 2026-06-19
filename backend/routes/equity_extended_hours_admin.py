"""Equity extended-hours override — Mongo-flippable RoadGuard relaxation.

Default behavior: RoadGuard blocks equity intents with `market_closed`
outside US RTH (M-F 9:30 AM – 4:00 PM ET). That's doctrine-correct
and what most operators want.

When flipped ON via this endpoint, RoadGuard accepts equity intents
during the wider Webull extended-hours window (M-F 4:00 AM – 8:00 PM
ET). Outside that window, RoadGuard still blocks. Holidays still
block. Weekends still block.

Operator's responsibility:
    Extended-hours spreads are wider and liquidity is thinner. Webull
    rejects market-orders outside its supported windows. Flipping
    this ON does NOT bypass any other RoadGuard check.

State persists in `runtime_flags._id="equity_extended_hours"`. Read
fresh per intent — no cache (one tiny Mongo read on equity intents
only).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth import get_current_user
from db import db


router = APIRouter(prefix="/admin/equity-extended-hours", tags=["equity-extended-hours"])

_FLAG_ID = "equity_extended_hours"


class _ToggleBody(BaseModel):
    enabled: bool


async def get_equity_extended_hours_enabled() -> bool:
    """Helper used by RoadGuard. Reads the Mongo flag; fail-CLOSED
    (defaults to False if the doc is missing or the read errors)."""
    try:
        doc = await db["runtime_flags"].find_one(
            {"_id": _FLAG_ID}, {"_id": 0, "enabled": 1},
        )
        return bool((doc or {}).get("enabled", False))
    except Exception:  # noqa: BLE001
        return False


@router.get("")
async def get_state(_user: dict = Depends(get_current_user)) -> dict:
    doc = await db["runtime_flags"].find_one({"_id": _FLAG_ID}, {"_id": 0})
    return {
        "enabled": bool((doc or {}).get("enabled", False)),
        "updated_at": (doc or {}).get("updated_at"),
        "updated_by": (doc or {}).get("updated_by"),
        "window_et": "04:00–20:00 M-F (Webull extended hours, holidays excluded)",
    }


@router.post("")
async def set_state(
    body: _ToggleBody,
    user: dict = Depends(get_current_user),
) -> dict:
    await db["runtime_flags"].update_one(
        {"_id": _FLAG_ID},
        {"$set": {
            "_id": _FLAG_ID,
            "enabled": bool(body.enabled),
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "updated_by": user.get("email") or "unknown",
        }},
        upsert=True,
    )
    return {"ok": True, "enabled": bool(body.enabled)}
