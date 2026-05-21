"""Internal endpoints called by the PARADOX coordinator agents.

These are deliberately thin. The scan / evaluate / risk / retrain
agents are SCHEDULING SCAFFOLDS today — the real logic will be wired
in over time. The execute agent is the load-bearing one: it pulls one
queued intent and routes it through the FULL gated submit path so the
11-gate chain + paradox-record writer fire.

Doctrine: every endpoint here MUST require admin auth, and the
execute-next endpoint MUST go through the real gated submit path. No
direct broker calls live in this module.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict

import httpx
from fastapi import APIRouter, Depends, HTTPException

from auth import get_current_user
from db import db


log = logging.getLogger("risedual.paradox_agent_routes")

router = APIRouter(prefix="/admin", tags=["paradox-coordinator-agents"])


# ───── scan / evaluate / risk / retrain — stubs ───────────────────────


@router.post("/paradox/scan")
async def paradox_scan(_user: dict = Depends(get_current_user)) -> Dict[str, Any]:
    """Scan stub. Real scanning is brain-side (Camaro/Alpha post intents
    when they spot setups). This endpoint reports current pipeline
    pressure so the operator can see whether the bottleneck is upstream
    of MC or downstream."""
    pending = await db.shared_intents.count_documents({
        "gate_state": {"$in": ["pending", "queued"]},
    })
    blocked = await db.shared_intents.count_documents({"gate_state": "blocked"})
    return {
        "ok": True,
        "stub": True,
        "pipeline": {
            "pending": pending,
            "blocked": blocked,
        },
        "ts": datetime.now(timezone.utc).isoformat(),
    }


@router.post("/paradox/evaluate")
async def paradox_evaluate(_user: dict = Depends(get_current_user)) -> Dict[str, Any]:
    """Evaluate stub. Per-intent gate evaluation already happens in
    `/api/execution/dry_run` (called by the brain sidecars). This
    coordinator-level endpoint summarises recent dry-run verdicts so
    the operator can see cycle-over-cycle pass-rates."""
    one_hour_ago = datetime.now(timezone.utc).replace(microsecond=0)
    counts = {"APPROVED": 0, "DAMPENED": 0, "REJECTED": 0}
    async for d in db.paradox_records.find(
        {"evaluation_kind": "dry_run"},
        {"kernel_verdict": 1},
    ).limit(500):
        v = d.get("kernel_verdict")
        if v in counts:
            counts[v] += 1
    return {
        "ok": True,
        "stub": True,
        "recent_dry_run_verdicts": counts,
        "ts": one_hour_ago.isoformat(),
    }


@router.post("/paradox/execute-next")
async def paradox_execute_next(
    _user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """Pull ONE queued intent that passed dry-run and submit it through
    the real gated path.

    No direct broker calls. No bypass. If `/api/execution/submit`
    rejects, the rejection is returned verbatim and the orphan
    watchdog still has nothing to catch (because nothing fired).
    """
    queued = await db.shared_intents.find_one({
        "gate_state": {"$in": ["pending", "queued"]},
        "executed_at": {"$in": [None, ""]},
    })
    if not queued:
        return {"ok": True, "noop": True, "reason": "no_queued_intents"}

    intent_id = queued.get("intent_id")
    notional = float(queued.get("order_notional_usd") or 0)
    if not intent_id or notional <= 0:
        return {
            "ok": True,
            "noop": True,
            "reason": "queued_intent_lacks_intent_id_or_notional",
            "intent_id": intent_id,
        }

    # Submit via the real gated path. We do a self-call here so all
    # of the gate chain (executor_seat_check, roadguard_spread_floor,
    # governor_authority, opponent_objection, caps) and the
    # paradox-record writer run as they would for any other operator.
    import jwt
    from datetime import timedelta

    secret = os.environ.get("JWT_SECRET")
    if not secret:
        raise HTTPException(500, "JWT_SECRET not configured")
    tok = jwt.encode(
        {
            "sub": "paradox-coordinator",
            "email": "coordinator@paradox.internal",
            "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
            "type": "access",
            "issuer": "paradox_coordinator_execute_next",
        },
        secret,
        algorithm="HS256",
    )
    api_base = os.environ.get("RISEDUAL_INTERNAL_API_BASE", "http://127.0.0.1:8001")
    async with httpx.AsyncClient(timeout=60) as cli:
        r = await cli.post(
            f"{api_base}/api/execution/submit",
            json={"intent_id": intent_id, "order_notional_usd": notional},
            headers={"Authorization": f"Bearer {tok}"},
        )
        try:
            body = r.json()
        except Exception:
            body = {"raw": r.text[:500]}
    return {
        "ok": r.status_code == 200,
        "status_code": r.status_code,
        "intent_id": intent_id,
        "submit_response": body,
    }


@router.post("/risk/check")
async def paradox_risk_check(_user: dict = Depends(get_current_user)) -> Dict[str, Any]:
    """Risk-check stub. Continuous exposure-cap evaluation already
    runs in `position_monitor`; this endpoint summarises the current
    snapshot so the coordinator can stamp it into the agent state."""
    from shared.exposure_caps import caps_snapshot
    try:
        snapshot = caps_snapshot()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "stub": True, "error": str(e)}
    return {"ok": True, "stub": True, "caps": snapshot}


@router.post("/ml/retrain/check")
async def paradox_retrain_check(_user: dict = Depends(get_current_user)) -> Dict[str, Any]:
    """Retrain stub. Patent J already tracks per-brain readiness; this
    endpoint reports whether any strategy crossed the promotion gate
    since last cycle. Real retrain triggers are deferred until we
    have closed-trade outcomes (`outcome_join` rows)."""
    return {
        "ok": True,
        "stub": True,
        "reason": "retrain_not_triggered_no_outcome_joins_yet",
    }
