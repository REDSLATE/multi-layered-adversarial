"""Admin control for exposure caps (per-order, per-day, open-notional).

Same Mongo-override pattern as `routes.webull_caps_admin`: an override
in `runtime_flags._id="exposure_caps_override"` wins over the deploy
env var so the operator can flip caps from the admin UI without
touching deploy config.

Endpoints:
    GET  /api/admin/exposure-caps/status   — effective caps + sources
    POST /api/admin/exposure-caps/set      — set one or more overrides
    POST /api/admin/exposure-caps/clear    — disable overrides

The 2026-06-18 motivation: Prod was hitting `cap_per_day=$50` two
hours before market open with no env-tweak path from a phone.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from auth import get_current_user
from db import db
from shared.exposure_caps import (
    CAP_OPEN_NOTIONAL_USD,
    CAP_PER_DAY_USD,
    CAP_PER_ORDER_USD,
    _CAPS_FLAG_DOC_ID,
    daily_spend_usd,
    effective_cap_open_notional_usd,
    effective_cap_per_day_usd,
    effective_cap_per_order_usd,
    refresh_cap_overrides_cache,
)


router = APIRouter(prefix="/admin/exposure-caps", tags=["exposure-caps-admin"])
_COLL = "runtime_flags"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SetCapsRequest(BaseModel):
    per_order_usd: Optional[float] = Field(None, gt=0, le=10_000_000)
    per_day_usd: Optional[float] = Field(None, gt=0, le=10_000_000)
    open_notional_usd: Optional[float] = Field(None, gt=0, le=10_000_000)
    reason: Optional[str] = Field(None, max_length=200)


def _env_value(key: str) -> Optional[float]:
    raw = (os.environ.get(key) or "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


@router.get("/status")
async def status(_user: dict = Depends(get_current_user)) -> Dict[str, Any]:
    """Return effective caps + all source contributions + 24h spend."""
    await refresh_cap_overrides_cache()
    doc = await db[_COLL].find_one({"_id": _CAPS_FLAG_DOC_ID}, {"_id": 0}) or {}
    spent = await daily_spend_usd()
    return {
        "effective": {
            "per_order_usd": effective_cap_per_order_usd(),
            "per_day_usd": effective_cap_per_day_usd(),
            "open_notional_usd": effective_cap_open_notional_usd(),
        },
        "live_state": {
            "daily_spend_usd": round(spent, 2),
            "remaining_per_day_usd": round(
                max(0.0, effective_cap_per_day_usd() - spent), 2,
            ),
        },
        "sources": {
            "mongo": {
                "enabled": bool(doc.get("enabled", False)),
                "per_order_usd": doc.get("per_order_usd"),
                "per_day_usd": doc.get("per_day_usd"),
                "open_notional_usd": doc.get("open_notional_usd"),
                "updated_at": doc.get("updated_at"),
                "updated_by": doc.get("updated_by"),
                "reason": doc.get("reason"),
            },
            "env": {
                "RISEDUAL_CAP_PER_ORDER_USD": _env_value("RISEDUAL_CAP_PER_ORDER_USD"),
                "RISEDUAL_CAP_PER_DAY_USD": _env_value("RISEDUAL_CAP_PER_DAY_USD"),
                "RISEDUAL_CAP_OPEN_NOTIONAL_USD": _env_value("RISEDUAL_CAP_OPEN_NOTIONAL_USD"),
            },
            "module_default": {
                "per_order_usd": CAP_PER_ORDER_USD,
                "per_day_usd": CAP_PER_DAY_USD,
                "open_notional_usd": CAP_OPEN_NOTIONAL_USD,
            },
        },
        "note": (
            "Mongo override wins over env, env over default. Set caps via "
            "POST /set to raise them from UI without redeploying."
        ),
    }


@router.post("/set")
async def set_caps(
    body: SetCapsRequest,
    user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """Set one or more cap overrides. Omitted fields keep their current value."""
    if (
        body.per_order_usd is None
        and body.per_day_usd is None
        and body.open_notional_usd is None
    ):
        raise HTTPException(
            status_code=400,
            detail="at least one of per_order_usd / per_day_usd / open_notional_usd required",
        )
    set_doc: Dict[str, Any] = {
        "enabled": True,
        "updated_at": _now(),
        "updated_by": user.get("email") or "operator",
        "reason": body.reason or "set via /admin/exposure-caps/set",
    }
    if body.per_order_usd is not None:
        set_doc["per_order_usd"] = float(body.per_order_usd)
    if body.per_day_usd is not None:
        set_doc["per_day_usd"] = float(body.per_day_usd)
    if body.open_notional_usd is not None:
        set_doc["open_notional_usd"] = float(body.open_notional_usd)
    await db[_COLL].update_one(
        {"_id": _CAPS_FLAG_DOC_ID},
        {"$set": set_doc},
        upsert=True,
    )
    await refresh_cap_overrides_cache()
    return {
        "ok": True,
        "effective": {
            "per_order_usd": effective_cap_per_order_usd(),
            "per_day_usd": effective_cap_per_day_usd(),
            "open_notional_usd": effective_cap_open_notional_usd(),
        },
        "flipped_at": set_doc["updated_at"],
    }


@router.post("/clear")
async def clear_caps(user: dict = Depends(get_current_user)) -> Dict[str, Any]:
    """Disable Mongo overrides. Caps fall back to env vars / defaults."""
    await db[_COLL].update_one(
        {"_id": _CAPS_FLAG_DOC_ID},
        {"$set": {
            "enabled": False,
            "updated_at": _now(),
            "updated_by": user.get("email") or "operator",
            "reason": "cleared via /admin/exposure-caps/clear",
        }},
        upsert=True,
    )
    await refresh_cap_overrides_cache()
    return {
        "ok": True,
        "effective": {
            "per_order_usd": effective_cap_per_order_usd(),
            "per_day_usd": effective_cap_per_day_usd(),
            "open_notional_usd": effective_cap_open_notional_usd(),
        },
    }
