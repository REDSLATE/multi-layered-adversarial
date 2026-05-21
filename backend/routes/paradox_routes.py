"""
PARADOX hierarchy — operator-facing routes.
============================================

Exposes the role anchors, role health (survival conditions), and
recent paradox_records so the Roster page can render the
role × runtime × health matrix.

Mount path (under `api_router` which prefixes `/api`):

    GET /api/admin/paradox/roster        — role × runtime × health
    GET /api/admin/paradox/records       — recent paradox_records
    GET /api/admin/paradox/health        — kernel + opponent_mode summary
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict

from fastapi import APIRouter, Depends, Query

from auth import get_current_user
from db import db
from namespaces import (
    OPPONENT_MODE_LIVE,
    OPPONENT_MODE_OFFLINE,
    OPPONENT_MODE_SHADOW,
    PARADOX_KERNEL,
    PARADOX_RECORDS,
    ROLE_ANCHORS,
    RUNTIME_ROLE,
)
from shared.runtime.role_health import evaluate_all_roles


router = APIRouter(prefix="/admin/paradox", tags=["paradox"])


@router.get("/roster")
async def paradox_roster(_user: dict = Depends(get_current_user)) -> Dict[str, Any]:
    """The 5-row role × runtime × health matrix.

    Replaces the old eligibility-matrix view with the PARADOX
    hierarchy's anchored model. Each role has exactly one runtime;
    drift is impossible without a code change (tripwired).
    """
    role_health = await evaluate_all_roles()
    rows = []
    for role, runtime in ROLE_ANCHORS.items():
        verdict = role_health.get(role, {})
        rows.append({
            "role": role,
            "runtime": runtime,
            "seat_status": verdict.get("seat_status", "unknown"),
            "healthy": verdict.get("healthy", False),
            "details": verdict,
        })
    return {
        "ok": True,
        "kernel": PARADOX_KERNEL,
        "auditor_doctrine": "emergent — paradox_record artifact, not a seat",
        "rows": rows,
    }


@router.get("/records")
async def paradox_records(
    hours: int = Query(default=24, ge=1, le=720),
    limit: int = Query(default=100, ge=1, le=500),
    _user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """Recent paradox_records — the audit artifact stamped on every
    gated intent. Shows the executor call alongside the opponent
    challenge (or shadow observation, or offline marker)."""
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    cursor = (
        db[PARADOX_RECORDS]
        .find({"created_at": {"$gte": since}})
        .sort("created_at", -1)
        .limit(limit)
    )
    items = []
    by_audit_status: Dict[str, int] = {"final": 0, "shadow": 0, "unaudited": 0}
    async for d in cursor:
        d.pop("_id", None)
        status = d.get("audit_status") or "unknown"
        by_audit_status[status] = by_audit_status.get(status, 0) + 1
        items.append(d)
    return {
        "ok": True,
        "since": since.isoformat(),
        "count": len(items),
        "by_audit_status": by_audit_status,
        "items": items,
    }


@router.get("/health")
async def paradox_health(_user: dict = Depends(get_current_user)) -> Dict[str, Any]:
    """Top-level kernel summary for the operator dashboard chip."""
    declared_mode = os.environ.get("OPPONENT_MODE", OPPONENT_MODE_SHADOW)
    if declared_mode not in {OPPONENT_MODE_LIVE, OPPONENT_MODE_SHADOW, OPPONENT_MODE_OFFLINE}:
        declared_mode = OPPONENT_MODE_OFFLINE
    role_health = await evaluate_all_roles()
    vacant = [r for r, v in role_health.items() if v.get("seat_status") == "vacant"]
    return {
        "ok": True,
        "kernel": PARADOX_KERNEL,
        "role_anchors": ROLE_ANCHORS,
        "runtime_role": RUNTIME_ROLE,
        "opponent_mode": declared_mode,
        "vacant_seats": vacant,
        "audit_status_implication": {
            OPPONENT_MODE_LIVE: "final",
            OPPONENT_MODE_SHADOW: "shadow",
            OPPONENT_MODE_OFFLINE: "unaudited",
        }[declared_mode],
    }
