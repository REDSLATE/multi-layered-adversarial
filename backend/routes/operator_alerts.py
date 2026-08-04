"""Operator alerts (2026-08-04) — in-app alert feed.

First producer: the Missed-Entry Ledger writes a `costly_miss` alert
whenever a blocked BUY would have hit take-profit. Permanent rows in
`operator_alerts` (small, idempotent by _id), acknowledgeable from
the UI. Deliberately generic so future producers (guardian trips,
tape failures) reuse the same feed.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth import get_current_user
from db import db

router = APIRouter(prefix="/admin/alerts", tags=["operator-alerts"])

COLLECTION = "operator_alerts"


@router.get("")
async def alerts_get(
    hours: int = 48,
    include_acked: bool = False,
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    hours = max(1, min(int(hours), 720))
    cut = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    q: dict = {"created_at": {"$gte": cut}}
    if not include_acked:
        q["acknowledged"] = False
    rows = await db[COLLECTION].find(q, {}).sort(
        "created_at", -1).max_time_ms(5000).to_list(50)
    unacked = await db[COLLECTION].count_documents(
        {"acknowledged": False}, maxTimeMS=5000)
    return {"ok": True, "alerts": rows, "unacked_total": unacked,
            "hours": hours}


class AckBody(BaseModel):
    alert_id: Optional[str] = None
    all: bool = False


@router.post("/ack")
async def alerts_ack(
    body: AckBody,
    user: dict = Depends(get_current_user),  # noqa: B008
):
    if not body.alert_id and not body.all:
        raise HTTPException(422, "alert_id or all=true required")
    stamp = {"acknowledged": True,
             "acked_at": datetime.now(timezone.utc).isoformat(),
             "acked_by": user.get("email") or "operator"}
    if body.all:
        res = await db[COLLECTION].update_many(
            {"acknowledged": False}, {"$set": stamp})
        return {"ok": True, "acked": res.modified_count}
    res = await db[COLLECTION].update_one(
        {"_id": body.alert_id}, {"$set": stamp})
    if res.matched_count == 0:
        raise HTTPException(404, "alert not found")
    return {"ok": True, "acked": 1}
