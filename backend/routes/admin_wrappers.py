"""Admin route — toggle legacy brain wrappers at runtime.

A/B diagnostic: when 403/502 rates spike on intent submit the
operator suspects the penalty-stacking wrappers are compressing
size_bias toward zero. This route lets the operator switch off the
wrapper for a SPECIFIC brain (one at a time) and observe whether
403/502 frequency drops by ~25% — confirming the multiplier effect.

Endpoints:
    GET  /api/admin/wrappers/status        — snapshot of which
                                              wrappers are enabled,
                                              who disabled them, why.
    POST /api/admin/wrappers/toggle        — set a brain's wrapper
                                              disabled/enabled. Audit-
                                              logged to Mongo.

Doctrine pin: env-var disables (`RISEDUAL_DISABLED_WRAPPERS`) take
precedence at process boot; operator-issued runtime overrides take
precedence over env at any given moment. Restart wipes runtime
overrides; env-var disables persist across restart.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from auth import get_current_user
from db import db
from shared.legacy_brain_wrappers import (
    BRAIN_WRAPPER_ASSIGNMENTS,
    set_wrapper_disabled,
    wrapper_status,
)


router = APIRouter(prefix="/admin/wrappers", tags=["admin-wrappers"])


WRAPPER_AUDIT_COLLECTION = "shared_wrapper_toggle_audit"


class WrapperToggleBody(BaseModel):
    brain_id: str = Field(..., description="camino | barracuda | hellcat | gto")
    disabled: bool = Field(..., description="true to disable the wrapper, false to re-enable")
    reason: str = Field(
        default="",
        max_length=500,
        description=(
            "required when disabling (min 4 chars) — short audit-trail "
            "note: e.g. 'A/B testing 403 cascade'"
        ),
    )


@router.get("/status")
async def wrappers_status(
    _user: dict = Depends(get_current_user),  # noqa: B008
) -> dict:
    """Snapshot of which brain wrappers are active vs disabled, plus
    the source of each disable (env vs operator override) and the
    operator's typed reason for runtime overrides."""
    return wrapper_status()


@router.post("/toggle")
async def wrappers_toggle(
    body: WrapperToggleBody,
    user: dict = Depends(get_current_user),  # noqa: B008
) -> dict:
    bid = body.brain_id.lower().strip()
    if bid not in BRAIN_WRAPPER_ASSIGNMENTS:
        raise HTTPException(
            status_code=422,
            detail=(
                f"unknown brain_id {body.brain_id!r}; expected one of "
                f"{sorted(BRAIN_WRAPPER_ASSIGNMENTS)}"
            ),
        )
    if body.disabled and len(body.reason.strip()) < 4:
        raise HTTPException(
            status_code=400,
            detail=(
                "disabling a wrapper requires a `reason` of ≥4 characters "
                "(audit-trail requirement)"
            ),
        )
    try:
        set_wrapper_disabled(bid, body.disabled, body.reason.strip())
    except ValueError as e:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=str(e)) from e

    # Mongo audit row — survives restart so the operator can see the
    # history of toggles (who, when, why, for which brain).
    await db[WRAPPER_AUDIT_COLLECTION].insert_one({
        "ts": datetime.now(timezone.utc).isoformat(),
        "by": user.get("email"),
        "brain_id": bid,
        "wrapper": BRAIN_WRAPPER_ASSIGNMENTS[bid],
        "disabled": body.disabled,
        "reason": body.reason.strip(),
    })

    return {"ok": True, "status": wrapper_status()}


@router.get("/audit")
async def wrappers_audit(
    _user: dict = Depends(get_current_user),  # noqa: B008
    limit: int = 50,
) -> dict:
    """Recent wrapper-toggle history — newest first."""
    limit = max(1, min(limit, 200))
    rows = await db[WRAPPER_AUDIT_COLLECTION].find(
        {}, {"_id": 0},
    ).sort("ts", -1).to_list(length=limit)
    return {"audit": rows, "count": len(rows)}
