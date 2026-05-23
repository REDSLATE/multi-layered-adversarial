"""Broker Freeze — admin HTTP endpoints.

GET  /api/admin/broker/freeze         — current state
POST /api/admin/broker/freeze         — flip ON  (body: {reason})
POST /api/admin/broker/thaw           — flip OFF (body: {reason})
GET  /api/admin/broker/freeze/history — audit trail
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from auth import get_current_user
from db import db
from namespaces import BROKER_FREEZE_AUDIT_LOG
from shared.broker_freeze import freeze as _freeze, get_freeze_state, thaw as _thaw


router = APIRouter(prefix="/admin/broker", tags=["broker-freeze"])


class FreezeIn(BaseModel):
    reason: str = Field(..., min_length=3, max_length=400)


class ThawIn(BaseModel):
    reason: Optional[str] = Field(default=None, max_length=400)


@router.get("/freeze")
async def freeze_state(_user: dict = Depends(get_current_user)):  # noqa: B008
    state = await get_freeze_state()
    return {
        **state,
        "doctrine_note": (
            "Broker freeze is the operator's stop-the-world kill switch. "
            "When ON, EVERY broker submit path raises BrokerFrozen — "
            "the lane toggles and gate chain are irrelevant. Use for "
            "audits, incidents, or post-bypass investigations."
        ),
    }


@router.post("/freeze")
async def freeze_set(
    body: FreezeIn,
    user: dict = Depends(get_current_user),  # noqa: B008
):
    actor = user.get("email") or "operator"
    try:
        state = await _freeze(body.reason, actor)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(e)) from e
    return {"ok": True, "frozen": True, "actor": actor, "state": state}


@router.post("/thaw")
async def thaw_set(
    body: ThawIn,
    user: dict = Depends(get_current_user),  # noqa: B008
):
    actor = user.get("email") or "operator"
    try:
        state = await _thaw(actor, body.reason)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(e)) from e
    return {"ok": True, "frozen": False, "actor": actor, "state": state}


@router.get("/freeze/history")
async def freeze_history(
    limit: int = 50,
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    rows = (
        await db[BROKER_FREEZE_AUDIT_LOG]
        .find({}, {"_id": 0})
        .sort("ts", -1)
        .to_list(min(max(limit, 1), 500))
    )
    return {"items": rows, "count": len(rows)}
