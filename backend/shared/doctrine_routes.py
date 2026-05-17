"""Doctrine Injection API.

Endpoints:
  GET    /api/admin/doctrine/overlays                    list active overlays
  POST   /api/admin/doctrine/overlays                    register an overlay
  DELETE /api/admin/doctrine/overlays/{overlay_id}       remove an overlay
  GET    /api/admin/doctrine/profile/{stack}             current runtime profile
                                                          (query: lane, regime, volatility, event_type)
  POST   /api/admin/doctrine/preset/{preset_id}          one-click ready-made overlay
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from auth import get_current_user
from shared.doctrine_injection import (
    DoctrineOverlay,
    SAFETY_INVARIANTS,
    WEIGHT_MAX,
    WEIGHT_MIN,
    build_crypto_breakout_overlay,
    build_fomc_overlay,
    get_engine,
)


router = APIRouter(tags=["doctrine"])


class OverlayIn(BaseModel):
    overlay_id: str = Field(..., min_length=1, max_length=64)
    lane: Optional[str] = None
    regime: Optional[str] = None
    volatility: Optional[str] = None
    event_type: Optional[str] = None
    expires_at: Optional[datetime] = None
    stack_weights: dict[str, float] = Field(default_factory=dict)
    governor_policy: dict[str, Any] = Field(default_factory=dict)
    personality_bias: dict[str, str] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


@router.get("/admin/doctrine/overlays")
async def list_overlays(_user: dict = Depends(get_current_user)):  # noqa: B008
    engine = get_engine()
    return {
        "active": engine.list_overlays(),
        "safety_invariants": SAFETY_INVARIANTS,
        "weight_clamp": {"min": WEIGHT_MIN, "max": WEIGHT_MAX},
    }


@router.post("/admin/doctrine/overlays")
async def register_overlay(body: OverlayIn, _user: dict = Depends(get_current_user)):  # noqa: B008
    engine = get_engine()
    overlay = DoctrineOverlay(**body.model_dump())
    try:
        engine.register_overlay(overlay)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"ok": True, "overlay_id": overlay.overlay_id, "active": engine.list_overlays()}


@router.delete("/admin/doctrine/overlays/{overlay_id}")
async def remove_overlay(overlay_id: str, _user: dict = Depends(get_current_user)):  # noqa: B008
    removed = get_engine().remove_overlay(overlay_id)
    if not removed:
        raise HTTPException(status_code=404, detail=f"overlay not found: {overlay_id}")
    return {"ok": True, "overlay_id": overlay_id}


@router.get("/admin/doctrine/profile/{stack}")
async def runtime_profile(
    stack: str,
    lane: str = Query(default="equity"),
    regime: Optional[str] = Query(default=None),
    volatility: Optional[str] = Query(default=None),
    event_type: Optional[str] = Query(default=None),
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    """Resolved runtime profile for `stack` under the current overlay
    set. Shows the operator exactly what shape the brain operates in
    for a given lane/regime/volatility/event combo."""
    # Doctrine pin (2026-02-17, rev3): governor-policy overlay attaches
    # based on the seat the brain currently holds, NOT brain identity.
    # Look up the governor seat assignment from the roster.
    from shared.roster import get_roster  # local import to avoid cycle
    roster = await get_roster()
    assignments = roster.get("assignments") or {}
    governor_seat = "crypto_governor" if lane == "crypto" else "governor"
    holds_governor_seat = assignments.get(governor_seat) == stack.lower()

    profile = get_engine().get_runtime_profile(
        stack_name=stack.lower(),
        lane=lane,
        regime=regime,
        volatility=volatility,
        event_type=event_type,
        holds_governor_seat=holds_governor_seat,
    )
    return {
        "stack": stack.lower(),
        "context": {
            "lane": lane, "regime": regime,
            "volatility": volatility, "event_type": event_type,
        },
        "profile": profile,
    }


_PRESETS = {
    "crypto_breakout_v1": build_crypto_breakout_overlay,
    "fomc_event_guard_v1": build_fomc_overlay,
}

@router.post("/admin/doctrine/preset/{preset_id}")
async def install_preset(preset_id: str, _user: dict = Depends(get_current_user)):  # noqa: B008
    builder = _PRESETS.get(preset_id)
    if not builder:
        raise HTTPException(
            status_code=404,
            detail=f"unknown preset: {preset_id}. Available: {list(_PRESETS)}",
        )
    overlay = builder()
    try:
        get_engine().register_overlay(overlay)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"ok": True, "overlay": overlay.to_dict()}
