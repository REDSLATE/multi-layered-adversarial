"""Admin endpoints to flip runtime system flags from the dashboard.

Operator pin (2026-02-23): Replaces the env-var-only flow for
`PARADOX_V3_BRAINS`, `PARADOX_V3_TRIGGER_WATCHER`, and
`PARADOX_V3_TRIGGER_REFIRE`. Before this router shipped, flipping
camino onto v3 required SSH access to the prod pod + a restart —
which the operator explicitly could not do from the dashboard on
mobile. After this router, the same flip is a single tap.

Routes (all require admin auth):

  GET  /api/admin/system-flags
  POST /api/admin/system-flags/paradox-v3-brains   body: {brains: []}
  POST /api/admin/system-flags/trigger-watcher     body: {enabled: bool}
  POST /api/admin/system-flags/trigger-refire      body: {enabled: bool}
  GET  /api/admin/system-flags/changes?limit=N

Audit: every mutation writes to `system_flag_changes`. The tile
surfaces the most recent ~5 rows.

Permission model: any logged-in admin can flip. Refire intentionally
sits behind the same admin gate today (matching watcher); if we later
want dual-sign on refire that's a follow-up — for the current
operator the bottleneck is "flipping at all", not granularity.
"""
from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from auth import get_current_user
from shared.system_flags import (
    effective_paradox_v3_brains,
    effective_trigger_refire_enabled,
    effective_trigger_watcher_enabled,
    get_system_flags,
    recent_flag_changes,
    refresh_system_flags,
    set_paradox_v3_brains,
    set_trigger_refire,
    set_trigger_watcher,
)


router = APIRouter(prefix="/admin/system-flags", tags=["admin-system-flags"])


# Restrict to a known, small whitelist so a malformed POST can't
# enable a runtime brain that doesn't actually exist. Drift here
# (e.g. a future "shelly" brain) is caught at the type boundary.
ALLOWED_BRAINS = {"camino", "barracuda", "hellcat", "gto"}


class V3BrainsBody(BaseModel):
    brains: List[str] = Field(default_factory=list)


class EnabledBody(BaseModel):
    enabled: bool


def _require_admin(user: dict) -> str:
    role = (user or {}).get("role")
    if role != "admin":
        raise HTTPException(status_code=403, detail="admin required")
    return (user.get("email") or "unknown").lower()


@router.get("")
async def get_flags(
    user: dict = Depends(get_current_user),  # noqa: B008
) -> Dict[str, Any]:
    """Read the current effective flag state.

    Returns BOTH the raw DB-backed snapshot AND the effective values
    after env-var fallback merging. The UI renders the effective
    values; the raw fields exist for diagnostics ("is this on
    because of DB or env?").
    """
    _require_admin(user)
    # Ensure the snapshot is fresh — admin reads bypass the 5s TTL
    # so the tile always shows the latest after a flip.
    await refresh_system_flags()
    snap = get_system_flags()
    return {
        "raw": {
            "paradox_v3_brains":         snap.paradox_v3_brains,
            "trigger_watcher_enabled":   snap.trigger_watcher_enabled,
            "trigger_refire_enabled":    snap.trigger_refire_enabled,
            "hydrated":                  snap.hydrated,
        },
        "effective": {
            "paradox_v3_brains":         effective_paradox_v3_brains(),
            "trigger_watcher_enabled":   effective_trigger_watcher_enabled(),
            "trigger_refire_enabled":    effective_trigger_refire_enabled(),
        },
        "allowed_brains": sorted(ALLOWED_BRAINS),
        "doctrine_note": (
            "DB-backed flags win over env vars. Empty list / False "
            "are EXPLICIT operator choices (DB wins). `null` means "
            "DB not set → env-var fallback applies. Effective "
            "values are what the brain runner actually reads."
        ),
    }


@router.post("/paradox-v3-brains")
async def post_paradox_v3_brains(
    body: V3BrainsBody,
    user: dict = Depends(get_current_user),  # noqa: B008
) -> Dict[str, Any]:
    """Set the brains-on-v3 list. Empty list = no brains on v3."""
    actor = _require_admin(user)
    requested = {str(b).strip().lower() for b in (body.brains or []) if str(b).strip()}
    invalid = requested - ALLOWED_BRAINS
    if invalid:
        raise HTTPException(
            status_code=400,
            detail=f"unknown brain ids: {sorted(invalid)}. "
                   f"allowed: {sorted(ALLOWED_BRAINS)}",
        )
    snap = await set_paradox_v3_brains(sorted(requested), actor=actor)
    return {
        "ok": True,
        "paradox_v3_brains": snap.paradox_v3_brains,
        "effective_paradox_v3_brains": effective_paradox_v3_brains(),
    }


@router.post("/trigger-watcher")
async def post_trigger_watcher(
    body: EnabledBody,
    user: dict = Depends(get_current_user),  # noqa: B008
) -> Dict[str, Any]:
    """Enable/disable the trigger watcher loop."""
    actor = _require_admin(user)
    snap = await set_trigger_watcher(bool(body.enabled), actor=actor)
    return {
        "ok": True,
        "trigger_watcher_enabled": snap.trigger_watcher_enabled,
        "effective_trigger_watcher_enabled": effective_trigger_watcher_enabled(),
    }


@router.post("/trigger-refire")
async def post_trigger_refire(
    body: EnabledBody,
    user: dict = Depends(get_current_user),  # noqa: B008
) -> Dict[str, Any]:
    """Enable/disable live re-firing of fired plans.

    Refire causes fired WAIT_FOR_TRIGGER plans to actually reach the
    broker (real money). The watcher must also be enabled for refire
    to do anything — surfaced in the doctrine note so the UI can
    warn before the operator flips.
    """
    actor = _require_admin(user)
    snap = await set_trigger_refire(bool(body.enabled), actor=actor)
    return {
        "ok": True,
        "trigger_refire_enabled": snap.trigger_refire_enabled,
        "effective_trigger_refire_enabled": effective_trigger_refire_enabled(),
    }


@router.get("/changes")
async def get_changes(
    limit: int = 20,
    user: dict = Depends(get_current_user),  # noqa: B008
) -> Dict[str, Any]:
    """Recent flag-change audit feed for the tile footer."""
    _require_admin(user)
    rows = await recent_flag_changes(limit=max(1, min(int(limit), 200)))
    return {"changes": rows, "count": len(rows)}
