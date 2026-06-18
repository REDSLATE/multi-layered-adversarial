"""Admin control for the Webull pre-trade notional floor.

Same pattern as `routes.unified_pipeline_admin`: a Mongo-backed
override that wins over the deploy env var so the operator can flip
the floor from the admin UI without touching deploy config.

Endpoints:
    GET  /api/admin/webull-caps/status     — current floor + sources
    POST /api/admin/webull-caps/set-floor  — override floor (JSON body)
    POST /api/admin/webull-caps/clear      — clear override, fall back to env/default

The Mongo doc lives at `runtime_flags._id="webull_min_notional_floor"`.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from auth import get_current_user
from db import db
from shared.broker.webull_caps import (
    DEFAULT_MIN_NOTIONAL_USD,
    _FLOOR_FLAG_DOC_ID,
    refresh_webull_floor_cache,
    webull_notional_band,
)


router = APIRouter(prefix="/admin/webull-caps", tags=["webull-caps-admin"])

_COLL = "runtime_flags"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SetFloorRequest(BaseModel):
    floor_usd: float = Field(..., gt=0, le=100.0, description="New floor in USD")
    reason: Optional[str] = Field(None, max_length=200)


@router.get("/status")
async def status(_user: dict = Depends(get_current_user)) -> Dict[str, Any]:
    """Return the effective Webull floor + ceiling and all source
    contributions (Mongo override, env var, default) so the operator
    can see exactly why the floor is what it is.
    """
    doc = await db[_COLL].find_one({"_id": _FLOOR_FLAG_DOC_ID}, {"_id": 0}) or {}
    env_raw = (os.environ.get("WEBULL_MIN_NOTIONAL_USD") or "").strip()
    env_value = None
    if env_raw:
        try:
            env_value = float(env_raw)
        except ValueError:
            env_value = None
    # Refresh first so the reported effective value matches what the
    # gate sees on the next pre-trade check.
    await refresh_webull_floor_cache()
    lo, hi, cap_source = webull_notional_band(buying_power_usd=None)
    return {
        "effective_floor_usd": lo,
        "effective_ceiling_usd": hi,
        "ceiling_source": cap_source,
        "sources": {
            "mongo": {
                "enabled": bool(doc.get("enabled", False)) and doc.get("floor_usd") is not None,
                "floor_usd": doc.get("floor_usd"),
                "updated_at": doc.get("updated_at"),
                "updated_by": doc.get("updated_by"),
                "reason": doc.get("reason"),
            },
            "env": {
                "set": env_raw != "",
                "value": env_value,
            },
            "default": DEFAULT_MIN_NOTIONAL_USD,
        },
        "note": (
            "Mongo override wins over env. Env wins over default. "
            "Set the Mongo override via POST /set-floor to flip the "
            "floor from the UI without redeploying."
        ),
    }


@router.post("/set-floor")
async def set_floor(
    body: SetFloorRequest,
    user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """Set the Mongo-backed floor override. Wins over env var."""
    if body.floor_usd <= 0:
        raise HTTPException(status_code=400, detail="floor_usd must be > 0")
    now = _now()
    await db[_COLL].update_one(
        {"_id": _FLOOR_FLAG_DOC_ID},
        {"$set": {
            "enabled": True,
            "floor_usd": float(body.floor_usd),
            "updated_at": now,
            "updated_by": user.get("email") or "operator",
            "reason": body.reason or "set via /admin/webull-caps/set-floor",
        }},
        upsert=True,
    )
    cached = await refresh_webull_floor_cache()
    lo, hi, _ = webull_notional_band(buying_power_usd=None)
    return {
        "ok": True,
        "cached_override_usd": cached,
        "effective_floor_usd": lo,
        "effective_ceiling_usd": hi,
        "flipped_at": now,
    }


@router.post("/clear")
async def clear(user: dict = Depends(get_current_user)) -> Dict[str, Any]:
    """Disable the Mongo override. Floor falls back to env var or default."""
    now = _now()
    await db[_COLL].update_one(
        {"_id": _FLOOR_FLAG_DOC_ID},
        {"$set": {
            "enabled": False,
            "updated_at": now,
            "updated_by": user.get("email") or "operator",
            "reason": "cleared via /admin/webull-caps/clear",
        }},
        upsert=True,
    )
    cached = await refresh_webull_floor_cache()
    lo, hi, _ = webull_notional_band(buying_power_usd=None)
    return {
        "ok": True,
        "cached_override_usd": cached,
        "effective_floor_usd": lo,
        "effective_ceiling_usd": hi,
        "cleared_at": now,
    }
