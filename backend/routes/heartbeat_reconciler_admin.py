"""Operator-facing routes for the heartbeat reconciler.

Exposes:
  POST /api/admin/heartbeat-reconcile/run
      Manually trigger one reconcile pass and return the summary.
      Useful for "I just deployed; tell me which brains needed bumps."

  GET /api/admin/heartbeat-reconcile/preview
      Dry-run version — returns what WOULD be bumped without writing.
      (Not yet implemented — placeholder for future use.)

Auth: admin JWT (matches all other /admin/* routes).
"""
from __future__ import annotations

import os
from typing import Any, Dict

from fastapi import APIRouter, Depends, Query

from auth import get_current_user
from shared.runtime.heartbeat_reconciler import (
    RECONCILER_MAX_AGE_SEC_DEFAULT,
    perform_reconcile,
)


router = APIRouter(prefix="/admin", tags=["heartbeat-reconciler"])


@router.post("/heartbeat-reconcile/run")
async def heartbeat_reconcile_run(
    max_age_sec: int = Query(
        default=RECONCILER_MAX_AGE_SEC_DEFAULT,
        ge=60,
        le=86_400,
        description=(
            "Refuse to reconcile from audit rows older than this. "
            "Stops us from accidentally bumping a heartbeat back to "
            "'fresh' using an ancient check-in. Default matches the "
            "worker's tick config."
        ),
    ),
    _user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """Manually trigger a reconciliation pass. Returns the same shape
    the background worker logs every minute. Operators use this after
    a deploy or when investigating a heartbeat anomaly."""
    return await perform_reconcile(max_age_sec=max_age_sec)


@router.get("/heartbeat-reconcile/status")
async def heartbeat_reconcile_status(
    _user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """Cheap config-view endpoint: shows current env knobs + whether
    the worker is enabled. Diagnostic value, doesn't touch DB."""
    return {
        "enabled": os.environ.get(
            "HEARTBEAT_RECONCILER_ENABLED", "true",
        ).strip().lower() in {"1", "true", "yes", "on"},
        "tick_sec": int(
            os.environ.get("HEARTBEAT_RECONCILER_TICK_SEC", "60"),
        ),
        "max_age_sec": int(
            os.environ.get(
                "HEARTBEAT_RECONCILER_MAX_AGE_S",
                str(RECONCILER_MAX_AGE_SEC_DEFAULT),
            ),
        ),
        "doctrine": "advisory_observability_only",
    }
