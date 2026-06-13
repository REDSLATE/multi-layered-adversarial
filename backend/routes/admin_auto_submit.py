"""Admin route — auto-submit policy toggle + status (2026-02-19).

Phase 1 of the throughput unlock. Operator can flip the
`tier_1_conservative` policy on/off without redeploying. The policy
respects EVERY gate (it just auto-clicks SUBMIT on intents that
already passed dry-run and meet the conservative checklist).
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from auth import get_current_user
from db import db
from shared.auto_submit_policy import (
    TIER_1_DEFAULTS,
    get_policy,
    set_policy,
)


router = APIRouter(prefix="/admin/auto-submit", tags=["admin-auto-submit"])


POLICY_AUDIT = "shared_auto_submit_policy_audit"


class PolicyBody(BaseModel):
    enabled: bool
    confidence_min: float | None = Field(default=None, ge=0.0, le=1.0)
    notional_default_usd: float | None = Field(default=None, gt=0.0)
    reason: str = Field(default="", max_length=400)


@router.get("/policy")
async def policy_status(_user: dict = Depends(get_current_user)) -> dict:
    """Current effective policy + defaults snapshot."""
    return {
        "policy": get_policy(),
        "defaults": TIER_1_DEFAULTS,
    }


@router.post("/policy")
async def policy_toggle(
    body: PolicyBody,
    user: dict = Depends(get_current_user),
) -> dict:
    if body.enabled and len(body.reason.strip()) < 4:
        raise HTTPException(
            status_code=400,
            detail=(
                "enabling auto-submit requires a `reason` of ≥4 characters "
                "(audit-trail requirement)"
            ),
        )
    overrides = {}
    if body.confidence_min is not None:
        overrides["confidence_min"] = body.confidence_min
    if body.notional_default_usd is not None:
        overrides["notional_default_usd"] = body.notional_default_usd
    policy = set_policy(enabled=body.enabled, **overrides)
    await db[POLICY_AUDIT].insert_one({
        "ts": datetime.now(timezone.utc).isoformat(),
        "by": user.get("email"),
        "enabled": body.enabled,
        "reason": body.reason.strip(),
        "overrides": overrides,
    })
    return {"ok": True, "policy": policy}


@router.get("/audit")
async def policy_audit(
    _user: dict = Depends(get_current_user),
    limit: int = 50,
) -> dict:
    limit = max(1, min(int(limit), 200))
    rows = await db[POLICY_AUDIT].find({}, {"_id": 0}).sort("ts", -1).to_list(length=limit)
    return {"audit": rows, "count": len(rows)}


@router.get("/recent-auto-trades")
async def recent_auto_trades(
    _user: dict = Depends(get_current_user),
    limit: int = 25,
) -> dict:
    """Show the last N receipts that were auto-submitted by tier-1."""
    limit = max(1, min(int(limit), 100))
    rows = await db["execution_receipts"].find(
        {"executed_by": "auto_submit_tier_1@risedual.io"},
        {"_id": 0},
    ).sort("executed_at", -1).to_list(length=limit)
    return {"receipts": rows, "count": len(rows)}
