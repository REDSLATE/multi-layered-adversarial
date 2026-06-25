"""Paradox v3 — admin observability endpoints (2026-02, Step 5).

Surfaces the operator-facing status of the v3 rollout in two routes:

  GET /api/admin/paradox-v3/status
      Both env flags + lifter-vs-emit posture so the operator can
      see at a glance which brains are on v3 and whether the
      trigger watcher is live.

  GET /api/admin/paradox-v3/watch-queue
      Watch-queue snapshot — state counts + the most-recent N rows.
      Safe to call when the watcher is dormant (read-only).

Doctrine: read-only. No writes, no broker calls, no env mutation.
"""
from __future__ import annotations

import os
from typing import Any, Dict

from fastapi import APIRouter, Depends, Query

from auth import get_current_user
from shared.pipeline.trigger_watcher import (
    is_refire_enabled,
    is_watcher_enabled,
    watch_queue_snapshot,
)


router = APIRouter(prefix="/admin/paradox-v3", tags=["admin-paradox-v3"])


@router.get("/status")
async def paradox_v3_status(
    _user: dict = Depends(get_current_user),  # noqa: B008
) -> Dict[str, Any]:
    """One-stop rollout status: which brains are on v3, whether the
    trigger watcher is live, and the in-process feature-flag posture.

    Operator workflow:
      * Empty `brains_on_v3` + watcher off → Steps 1-4 shipped, no
        brain emitting v3 yet. Use this to confirm posture BEFORE
        flipping camino on.
      * `brains_on_v3=["camino"]` + watcher off → Step 4 shadow
        running. Wait 24h, check `plan_discipline` axis on Camino's
        report card.
      * `brains_on_v3=["camino"]` + watcher on → Step 5 LIVE.
        Camino's WAIT_FOR_TRIGGER plans land on the queue; the
        watcher fires/invalidates/expires them.

    Lane posture (2026-02-22): SeatPolicy's auth gates run BEFORE
    the WAIT short-circuit (defensive doctrine). A vacant executor
    seat for a lane means WAIT plans on THAT lane cannot be parked.
    `lane_executor_seats` surfaces the current holder so the operator
    knows which lanes are eligible for v3 WAIT plans.
    """
    brains_csv = os.environ.get("PARADOX_V3_BRAINS", "").strip()
    brains_on_v3 = (
        sorted({b.strip().lower() for b in brains_csv.split(",") if b.strip()})
        if brains_csv else []
    )

    # Lane-aware seat-holder posture (read-only, defensive).
    from db import db
    from namespaces import BRAIN_ROSTER
    lane_seats: Dict[str, Any] = {
        "equity": {"executor_holder": None, "wait_plans_eligible": False},
        "crypto": {"executor_holder": None, "wait_plans_eligible": False},
    }
    try:
        roster = await db[BRAIN_ROSTER].find_one(
            {"_id": "current"}, {"_id": 0, "assignments": 1},
        ) or {}
        assignments = roster.get("assignments") or {}
        equity_holder = assignments.get("executor")
        crypto_holder = assignments.get("crypto")
        lane_seats["equity"]["executor_holder"] = equity_holder
        lane_seats["equity"]["wait_plans_eligible"] = bool(equity_holder)
        lane_seats["crypto"]["executor_holder"] = crypto_holder
        lane_seats["crypto"]["wait_plans_eligible"] = bool(crypto_holder)
    except Exception:  # noqa: BLE001
        pass

    return {
        "brains_on_v3": brains_on_v3,
        "trigger_watcher_enabled": is_watcher_enabled(),
        "trigger_refire_enabled": is_refire_enabled(),
        "flags": {
            "PARADOX_V3_BRAINS": brains_csv or None,
            "PARADOX_V3_TRIGGER_WATCHER": (
                os.environ.get("PARADOX_V3_TRIGGER_WATCHER") or None
            ),
            "PARADOX_V3_TRIGGER_REFIRE": (
                os.environ.get("PARADOX_V3_TRIGGER_REFIRE") or None
            ),
        },
        "rollout_step": _infer_rollout_step(
            brains_on_v3, is_watcher_enabled(), is_refire_enabled(),
        ),
        "lane_executor_seats": lane_seats,
        "doctrine_note": (
            "Step 5 LIVE = at least one brain in `brains_on_v3` AND "
            "`trigger_watcher_enabled=true`. Step 5.b REFIRE = the "
            "above plus `trigger_refire_enabled=true` — fired plans "
            "translate into actual broker calls. Step 4 SHADOW = "
            "brains on v3 but watcher still off. Steps 1-3 = no "
            "brain on v3. WAIT plans can only be parked on lanes "
            "whose `wait_plans_eligible=true` (executor seat held)."
        ),
    }


def _infer_rollout_step(
    brains: list[str], watcher_live: bool, refire_live: bool,
) -> str:
    if not brains:
        return "steps_1_to_3_rails_only"
    if brains and not watcher_live:
        return "step_4_shadow_emit_only"
    if watcher_live and not refire_live:
        return "step_5_trigger_watcher_live"
    return "step_5b_refire_live"


@router.get("/watch-queue")
async def paradox_v3_watch_queue(
    limit: int = Query(default=50, ge=1, le=500),
    _user: dict = Depends(get_current_user),  # noqa: B008
) -> Dict[str, Any]:
    """Read-only snapshot of `intent_watch_queue`.

    Returns:
        {
            "enabled":   bool,                   # watcher env flag
            "counts":    {watching, fired, invalidated, expired},
            "recent":    [last N rows desc by queued_at, _id stripped],
            "fetched_at": iso
        }

    Even when the watcher is dormant this endpoint is useful — it
    surfaces any backlog the operator would drain by flipping the
    flag on.
    """
    return await watch_queue_snapshot(limit=limit)
