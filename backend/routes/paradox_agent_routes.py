"""Internal endpoints called by the PARADOX coordinator.

Doctrine pin (2026-02-XX, v0):
    Coordinator v0 = candidate generator + advisory evaluator only.
    NO execution authority. NO auto-submit to broker. Everything
    writes to `paradox_candidates` / `paradox_records` first;
    the existing 11-gate chain + human/admin promotion are still
    required for execution.

    `execute-next` is the EXCEPTION — it intentionally goes through
    the real gated submit path. It pulls one already-queued intent
    (an intent that some BRAIN already produced and gates already
    let through to `queued`/`pending` state) and routes it through
    `/api/execution/submit`. The coordinator does NOT bypass the
    chain; it just nudges the queue along.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth import get_current_user
from db import db
from services.paradox_evaluator import evaluate_candidate
from services.paradox_retrain import check_retrain
from services.paradox_risk import check_candidate, check_global
from services.paradox_scanner import run_scan


log = logging.getLogger("risedual.paradox_agent_routes")

router = APIRouter(prefix="/admin", tags=["paradox-coordinator-agents"])


# ─── /paradox/scan ────────────────────────────────────────────────────


class ScanRequest(BaseModel):
    snapshots: Optional[Dict[str, Dict[str, Any]]] = None
    universe_override: Optional[List[Dict[str, str]]] = None


@router.post("/paradox/scan")
async def paradox_scan(
    body: Optional[ScanRequest] = None,
    _user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """Walk the watchlist + filters, persist candidates.

    Output rows go to `paradox_candidates`. NO trade intents
    produced. Snapshots are operator-supplied — the scanner does
    not invent market data.
    """
    body = body or ScanRequest()
    return await run_scan(
        snapshots=body.snapshots,
        universe_override=body.universe_override,
    )


# ─── /paradox/evaluate ────────────────────────────────────────────────


class EvaluateRequest(BaseModel):
    candidate_id: str


@router.post("/paradox/evaluate")
async def paradox_evaluate(
    body: EvaluateRequest,
    _user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """LLM-driven evaluation: strategist + opponent + auditor.

    Writes a `paradox_records` row of kind
    `paradox_v0_evaluation`. Does NOT post to /api/execution/submit
    on success — human/admin promotion still required.
    """
    try:
        return await evaluate_candidate(candidate_id=body.candidate_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


# ─── /paradox/risk/check ──────────────────────────────────────────────


class RiskCheckRequest(BaseModel):
    candidate_id: Optional[str] = None


@router.post("/risk/check")
async def paradox_risk_check(
    body: Optional[RiskCheckRequest] = None,
    _user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """Per-candidate + global risk gate.

    If `candidate_id` is supplied → check_candidate (stamps the
    candidate `risk_blocked` if any gate fails and writes a
    paradox_record audit row).
    If omitted → return the global state only.
    """
    body = body or RiskCheckRequest()
    if body.candidate_id:
        try:
            return await check_candidate(candidate_id=body.candidate_id)
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))
    return {"ok": True, "global": await check_global()}


# ─── /paradox/ml/retrain/check ────────────────────────────────────────


class RetrainCheckRequest(BaseModel):
    force_recommend: bool = False


@router.post("/ml/retrain/check")
async def paradox_retrain_check(
    body: Optional[RetrainCheckRequest] = None,
    _user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """Evaluate retrain triggers; persist a recommendation row
    IFF any trigger fires. Does NOT auto-train and does NOT
    promote local/self_trained — operator-only."""
    body = body or RetrainCheckRequest()
    return await check_retrain(force_recommend=body.force_recommend)


# ─── /paradox/execute-next ────────────────────────────────────────────


@router.post("/paradox/execute-next")
async def paradox_execute_next(
    _user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """Pull ONE queued intent (produced by a BRAIN, already past
    dry-run) and submit it through the real gated path.

    No direct broker calls. No bypass. If `/api/execution/submit`
    rejects, the rejection is returned verbatim and the orphan
    watchdog still has nothing to catch (because nothing fired).

    NOTE: Coordinator v0 doctrine — this endpoint does NOT
    promote `paradox_records` rows. It only flushes the existing
    intent queue. Promotion from a paradox_record to a tradeable
    intent is a separate human/admin step.
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
        except Exception:  # noqa: BLE001
            body = {"raw": r.text[:500]}
    return {
        "ok": r.status_code == 200,
        "status_code": r.status_code,
        "intent_id": intent_id,
        "submit_response": body,
    }
