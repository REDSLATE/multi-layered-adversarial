"""Gain Goal admin — operator profit objectives per lane.

GET  /api/admin/gain-goals            config + per-lane status + rollup
POST /api/admin/gain-goals/config     merge-update config, re-evaluate
POST /api/admin/gain-goals/ack        acknowledge a drawdown breach
POST /api/admin/gain-goals/evaluate   force an evaluation cycle
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from db import db
from auth import get_current_user
from shared.goals import gain_goal, worker

router = APIRouter(prefix="/admin/gain-goals", tags=["gain-goals"])

_TARGET_KEYS = {
    "enabled", "target_net_pnl_usd", "target_return_pct",
    "maximum_window_drawdown_usd", "maximum_window_drawdown_pct",
    "minimum_resolved_trades", "session_scope", "window_type",
    "rolling_days", "custom_start", "custom_end", "primary_metric",
    "and_condition",
}
_DEFAULTS_KEYS = set(gain_goal.DEFAULTS["defaults"].keys())
_THROTTLE_KEYS = set(gain_goal.DEFAULTS["ahead_of_pace_throttle"].keys())


class GoalConfigBody(BaseModel):
    enabled: Optional[bool] = None
    defaults: Optional[dict] = None
    equity: Optional[dict] = None
    crypto: Optional[dict] = None
    ahead_of_pace_throttle: Optional[dict] = None


class AckBody(BaseModel):
    lane: str


@router.get("")
async def gain_goal_status(_user: dict = Depends(get_current_user)):  # noqa: B008
    return await worker.evaluate_and_publish()


@router.post("/config")
async def set_gain_goal_config(
    body: GoalConfigBody,
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    update: dict[str, Any] = {}
    if body.enabled is not None:
        update["enabled"] = bool(body.enabled)
    for lane in ("equity", "crypto"):
        payload = getattr(body, lane)
        if payload:
            bad = set(payload) - _TARGET_KEYS
            if bad:
                raise HTTPException(422, f"unknown {lane} keys: {sorted(bad)}")
            for k, v in payload.items():
                update[f"{lane}.{k}"] = v
    if body.defaults:
        bad = set(body.defaults) - _DEFAULTS_KEYS
        if bad:
            raise HTTPException(422, f"unknown defaults keys: {sorted(bad)}")
        for k, v in body.defaults.items():
            update[f"defaults.{k}"] = v
    if body.ahead_of_pace_throttle:
        bad = set(body.ahead_of_pace_throttle) - _THROTTLE_KEYS
        if bad:
            raise HTTPException(422, f"unknown throttle keys: {sorted(bad)}")
        for k, v in body.ahead_of_pace_throttle.items():
            update[f"ahead_of_pace_throttle.{k}"] = v
    if not update:
        raise HTTPException(422, "no config fields provided")
    update["updated_by"] = _user.get("email") or "unknown"
    from datetime import datetime, timezone
    update["updated_at"] = datetime.now(timezone.utc).isoformat()
    await db["runtime_flags"].update_one(
        {"_id": gain_goal.FLAG_ID}, {"$set": update}, upsert=True,
    )
    return await worker.evaluate_and_publish()


@router.post("/ack")
async def ack_breach(
    body: AckBody,
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    lane = (body.lane or "").lower()
    if lane not in gain_goal.LANES:
        raise HTTPException(422, f"invalid lane: {body.lane}")
    return await worker.acknowledge_breach(lane, _user.get("email") or "unknown")


@router.post("/evaluate")
async def force_evaluate(_user: dict = Depends(get_current_user)):  # noqa: B008
    return await worker.evaluate_and_publish()
