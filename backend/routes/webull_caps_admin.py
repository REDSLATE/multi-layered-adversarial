"""Admin control for the Webull pre-trade notional floor.

Same pattern as `routes.unified_pipeline_admin`: a Mongo-backed
override that wins over the deploy env var so the operator can flip
the floor from the admin UI without touching deploy config.

Endpoints:
    GET  /api/admin/webull-caps/status         — current floor + pct + sources
    POST /api/admin/webull-caps/set-floor      — override floor (JSON body)
    POST /api/admin/webull-caps/set-pct        — override pct-of-buying-power
    POST /api/admin/webull-caps/clear          — clear floor override
    POST /api/admin/webull-caps/clear-pct      — clear pct override

The Mongo docs live at:
    `runtime_flags._id="webull_min_notional_floor"`  (floor)
    `runtime_flags._id="webull_pct_of_buying_power"` (pct)
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
    DEFAULT_PCT_OF_BUYING_POWER,
    _FLOOR_FLAG_DOC_ID,
    _PCT_FLAG_DOC_ID,
    refresh_webull_floor_cache,
    refresh_webull_pct_cache,
    webull_notional_band,
    webull_pct_of_buying_power,
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
    """Return the effective Webull floor + ceiling + pct-of-BP and
    all source contributions (Mongo override, env var, default) so
    the operator can see exactly why each value is what it is.
    """
    floor_doc = await db[_COLL].find_one({"_id": _FLOOR_FLAG_DOC_ID}, {"_id": 0}) or {}
    pct_doc = await db[_COLL].find_one({"_id": _PCT_FLAG_DOC_ID}, {"_id": 0}) or {}
    env_raw = (os.environ.get("WEBULL_MIN_NOTIONAL_USD") or "").strip()
    env_value = None
    if env_raw:
        try:
            env_value = float(env_raw)
        except ValueError:
            env_value = None
    env_pct_raw = (os.environ.get("WEBULL_PCT_OF_BUYING_POWER") or "").strip()
    env_pct_value = None
    if env_pct_raw:
        try:
            env_pct_value = float(env_pct_raw)
        except ValueError:
            env_pct_value = None
    # Refresh both caches first so the reported effective values
    # match what the gate sees on the next pre-trade check.
    await refresh_webull_floor_cache()
    await refresh_webull_pct_cache()
    lo, hi, cap_source = webull_notional_band(buying_power_usd=None)
    return {
        "effective_floor_usd": lo,
        "effective_ceiling_usd": hi,
        "ceiling_source": cap_source,
        "effective_pct_of_buying_power": webull_pct_of_buying_power(),
        "sources": {
            "mongo": {
                "enabled": bool(floor_doc.get("enabled", False)) and floor_doc.get("floor_usd") is not None,
                "floor_usd": floor_doc.get("floor_usd"),
                "updated_at": floor_doc.get("updated_at"),
                "updated_by": floor_doc.get("updated_by"),
                "reason": floor_doc.get("reason"),
            },
            "env": {
                "set": env_raw != "",
                "value": env_value,
            },
            "default": DEFAULT_MIN_NOTIONAL_USD,
        },
        "pct_sources": {
            "mongo": {
                "enabled": bool(pct_doc.get("enabled", False)) and pct_doc.get("pct") is not None,
                "pct": pct_doc.get("pct"),
                "updated_at": pct_doc.get("updated_at"),
                "updated_by": pct_doc.get("updated_by"),
                "reason": pct_doc.get("reason"),
            },
            "env": {
                "set": env_pct_raw != "",
                "value": env_pct_value,
            },
            "default": DEFAULT_PCT_OF_BUYING_POWER,
        },
        "note": (
            "Mongo override wins over env. Env wins over default. "
            "Set via POST /set-floor or /set-pct to flip without "
            "redeploying."
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


class SetPctRequest(BaseModel):
    pct: float = Field(
        ..., gt=0.0, le=1.0,
        description=(
            "New pct-of-buying-power. Must be in (0, 1.0]. "
            "0.25 = 25% of buying power per order."
        ),
    )
    reason: Optional[str] = Field(None, max_length=200)


@router.post("/set-pct")
async def set_pct(
    body: SetPctRequest,
    user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """Set the Mongo-backed pct-of-buying-power override. Wins over
    `WEBULL_PCT_OF_BUYING_POWER` env. Takes effect within 5s on the
    pre-trade gate (cache TTL). Hard sanity ceiling ($500/order)
    still applies regardless of this value."""
    now = _now()
    await db[_COLL].update_one(
        {"_id": _PCT_FLAG_DOC_ID},
        {"$set": {
            "enabled": True,
            "pct": float(body.pct),
            "updated_at": now,
            "updated_by": user.get("email") or "operator",
            "reason": body.reason or "set via /admin/webull-caps/set-pct",
        }},
        upsert=True,
    )
    cached = await refresh_webull_pct_cache()
    return {
        "ok": True,
        "cached_override_pct": cached,
        "effective_pct_of_buying_power": webull_pct_of_buying_power(),
        "flipped_at": now,
    }


@router.post("/clear-pct")
async def clear_pct(user: dict = Depends(get_current_user)) -> Dict[str, Any]:
    """Disable the Mongo pct override. Pct falls back to env var
    or default (10%)."""
    now = _now()
    await db[_COLL].update_one(
        {"_id": _PCT_FLAG_DOC_ID},
        {"$set": {
            "enabled": False,
            "updated_at": now,
            "updated_by": user.get("email") or "operator",
            "reason": "cleared via /admin/webull-caps/clear-pct",
        }},
        upsert=True,
    )
    cached = await refresh_webull_pct_cache()
    return {
        "ok": True,
        "cached_override_pct": cached,
        "effective_pct_of_buying_power": webull_pct_of_buying_power(),
        "cleared_at": now,
    }
