"""Paradox v2 API surface — `/api/v2/*`.

Stand-alone deployment (2026-02-19): operator-driven endpoints only.
Not wired into the live intent pipeline. After ≥50 manual evaluations
prove the seat-policy concept holds, flip the wire in intents.py.
"""
from __future__ import annotations

from typing import Optional, Literal
from datetime import datetime, timezone
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from auth import get_current_user
from db import db
from namespaces import (
    PARADOX_V2_BRAIN_REGISTRY,
    PARADOX_V2_EVALUATIONS,
    PARADOX_V2_GOVERNOR_RULES,
    PARADOX_V2_PROMOTION_LOG,
    PARADOX_V2_ROADGUARD_STOPS,
    PARADOX_V2_SEAT_PERFORMANCE,
    PARADOX_V2_SEAT_POLICY,
    PARADOX_V2_SEAT_TRUSTED,
)
from shared.paradox_v2.evaluator import evaluate as evaluate_pipeline
from shared.paradox_v2.seed import seed_paradox_v2


router = APIRouter(prefix="/v2", tags=["paradox_v2"])


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ─── /v2/seed ─────────────────────────────────────────────────────────


@router.post("/seed")
async def post_seed(_user: dict = Depends(get_current_user)) -> dict:
    """Idempotent seed of the four canonical brains + default seat
    policies + default governor rules. Safe to re-run."""
    return await seed_paradox_v2()


# ─── /v2/evaluate ─────────────────────────────────────────────────────


class EvaluateRequest(BaseModel):
    seat_id: str = Field(..., description="canonical seat id (e.g. equity_executor)")
    brain_id: str
    symbol: str
    lane: Literal["equity", "crypto"]
    action: Literal["BUY", "SELL", "HOLD"]
    confidence: float = Field(..., ge=0.0, le=1.0)
    suggested_notional_usd: float = Field(..., ge=0.0)
    evidence: dict = Field(default_factory=dict)


@router.post("/evaluate")
async def post_evaluate(
    body: EvaluateRequest,
    _user: dict = Depends(get_current_user),
) -> dict:
    """Run a single brain opinion through the five-layer pipeline.
    Returns the full receipt — also persisted for replay/audit.
    Does NOT submit any order; the caller decides what to do with
    an `EXECUTED` decision."""
    opinion = {
        "brain_id": body.brain_id,
        "symbol": body.symbol.upper().strip(),
        "lane": body.lane,
        "action": body.action,
        "confidence": body.confidence,
        "suggested_notional_usd": body.suggested_notional_usd,
        "evidence": body.evidence,
        "emitted_at": _now(),
    }
    return await evaluate_pipeline(opinion, body.seat_id)


# ─── /v2/state (read-only dashboard endpoint) ─────────────────────────


@router.get("/state")
async def get_state(_user: dict = Depends(get_current_user)) -> dict:
    """Snapshot of every collection — what the operator dashboard reads."""
    brains = await db[PARADOX_V2_BRAIN_REGISTRY].find({}, {"_id": 0}).to_list(50)
    policies = await db[PARADOX_V2_SEAT_POLICY].find({}, {"_id": 0}).to_list(50)
    trust = await db[PARADOX_V2_SEAT_TRUSTED].find({}, {"_id": 0}).to_list(200)
    rules = await db[PARADOX_V2_GOVERNOR_RULES].find({}, {"_id": 0}).to_list(50)
    stops = await db[PARADOX_V2_ROADGUARD_STOPS].find(
        {"is_active": True, "cleared_at": None}, {"_id": 0},
    ).sort("created_at", -1).to_list(50)
    perf = await db[PARADOX_V2_SEAT_PERFORMANCE].find({}, {"_id": 0}).sort(
        "window_start", -1,
    ).to_list(20)
    recent = await db[PARADOX_V2_EVALUATIONS].find({}, {"_id": 0}).sort(
        "ts", -1,
    ).to_list(20)
    promotions = await db[PARADOX_V2_PROMOTION_LOG].find({}, {"_id": 0}).sort(
        "ts", -1,
    ).to_list(20)
    return {
        "brains": brains,
        "seat_policies": policies,
        "trust": trust,
        "governor_rules": rules,
        "active_stops": stops,
        "performance": perf,
        "recent_evaluations": recent,
        "promotion_log": promotions,
        "doctrine": (
            "Brain owns doctrine. Seat owns execution. Governor owns modifiers. "
            "RoadGuard owns stops. Verifier owns promotion."
        ),
    }


# ─── /v2/seat-trust ───────────────────────────────────────────────────


class TrustUpsertRequest(BaseModel):
    seat_id: str
    brain_id: str
    trust_level: float = Field(1.0, ge=0.0, le=1.0)


@router.post("/seat-trust")
async def post_seat_trust(
    body: TrustUpsertRequest,
    user: dict = Depends(get_current_user),
) -> dict:
    """Add/update a (seat_id, brain_id) trust row."""
    now = _now()
    await db[PARADOX_V2_SEAT_TRUSTED].update_one(
        {"seat_id": body.seat_id, "brain_id": body.brain_id},
        {
            "$set": {
                "trust_level": body.trust_level,
                "added_at": now,
                "added_by": user.get("email") or "operator",
            },
        },
        upsert=True,
    )
    return {"ok": True, "seat_id": body.seat_id, "brain_id": body.brain_id,
            "trust_level": body.trust_level}


@router.delete("/seat-trust")
async def delete_seat_trust(
    seat_id: str = Query(...),
    brain_id: str = Query(...),
    _user: dict = Depends(get_current_user),
) -> dict:
    r = await db[PARADOX_V2_SEAT_TRUSTED].delete_one(
        {"seat_id": seat_id, "brain_id": brain_id},
    )
    return {"ok": True, "deleted": r.deleted_count}


# ─── /v2/seat-policy ──────────────────────────────────────────────────


class SeatPolicyPatch(BaseModel):
    """All fields optional — only provided fields are updated."""
    autonomy_mode: Optional[Literal["observe", "shadow", "toehold", "auto_execute"]] = None
    enabled: Optional[bool] = None
    max_notional_usd: Optional[float] = Field(None, ge=0.0)
    size_multiplier: Optional[float] = Field(None, ge=0.0, le=2.0)
    daily_risk_budget_usd: Optional[float] = Field(None, ge=0.0)
    max_position_count: Optional[int] = Field(None, ge=0)
    max_concentration_pct: Optional[float] = Field(None, ge=0.0, le=100.0)
    confidence_min: Optional[float] = Field(None, ge=0.0, le=1.0)
    market_quality_min: Optional[float] = Field(None, ge=0.0, le=1.0)
    max_auditor_objections: Optional[int] = Field(None, ge=0)
    required_governor_stance: Optional[Literal["RISK_DOWN", "NEUTRAL", "RISK_UP"]] = None
    reason: str = Field(..., min_length=4, description="audit reason ≥4 chars")


@router.patch("/seat-policy/{seat_id}")
async def patch_seat_policy(
    seat_id: str,
    body: SeatPolicyPatch,
    user: dict = Depends(get_current_user),
) -> dict:
    """Update one or more fields on a seat policy. Audit-logged.

    autonomy_mode transitions are ALSO mirrored into the promotion log.
    """
    current = await db[PARADOX_V2_SEAT_POLICY].find_one({"seat_id": seat_id}, {"_id": 0})
    if not current:
        raise HTTPException(status_code=404, detail=f"unknown seat: {seat_id}")
    patch = body.model_dump(exclude_unset=True, exclude={"reason"})
    if not patch:
        return {"ok": True, "no_changes": True, "policy": current}
    now = _now()
    patch["updated_at"] = now
    patch["updated_by"] = user.get("email") or "operator"
    await db[PARADOX_V2_SEAT_POLICY].update_one(
        {"seat_id": seat_id}, {"$set": patch},
    )
    # If autonomy_mode changed, append to promotion log.
    if "autonomy_mode" in patch and patch["autonomy_mode"] != current.get("autonomy_mode"):
        await db[PARADOX_V2_PROMOTION_LOG].insert_one({
            "promotion_id": str(uuid.uuid4()),
            "seat_id": seat_id,
            "from_mode": current.get("autonomy_mode"),
            "to_mode": patch["autonomy_mode"],
            "reason": body.reason,
            "triggered_by": user.get("email") or "operator",
            "metrics_snapshot": {},
            "ts": now,
        })
    new = await db[PARADOX_V2_SEAT_POLICY].find_one({"seat_id": seat_id}, {"_id": 0})
    return {"ok": True, "policy": new}


# ─── /v2/roadguard ────────────────────────────────────────────────────


class RoadGuardRaiseRequest(BaseModel):
    seat_id: str
    reason: str = Field(..., min_length=4)


@router.post("/roadguard/raise")
async def post_raise_stop(
    body: RoadGuardRaiseRequest,
    user: dict = Depends(get_current_user),
) -> dict:
    """Raise a binary STOP on a seat. While active, evaluate() returns
    REJECTED_ROADGUARD for every opinion routed through that seat."""
    now = _now()
    doc = {
        "stop_id": str(uuid.uuid4()),
        "seat_id": body.seat_id,
        "is_active": True,
        "reason": body.reason,
        "triggered_by": user.get("email") or "operator",
        "created_at": now,
        "cleared_at": None,
        "cleared_by": None,
    }
    await db[PARADOX_V2_ROADGUARD_STOPS].insert_one(dict(doc))
    doc.pop("_id", None)
    return {"ok": True, "stop": doc}


class RoadGuardClearRequest(BaseModel):
    seat_id: str
    reason: str = Field(..., min_length=4)


@router.post("/roadguard/clear")
async def post_clear_stop(
    body: RoadGuardClearRequest,
    user: dict = Depends(get_current_user),
) -> dict:
    """Clear ALL active stops on a seat."""
    now = _now()
    r = await db[PARADOX_V2_ROADGUARD_STOPS].update_many(
        {"seat_id": body.seat_id, "is_active": True, "cleared_at": None},
        {"$set": {
            "is_active": False,
            "cleared_at": now,
            "cleared_by": user.get("email") or "operator",
        }},
    )
    return {"ok": True, "cleared": r.modified_count, "reason": body.reason}


# ─── /v2/evaluations (audit feed) ─────────────────────────────────────


@router.get("/evaluations")
async def get_evaluations(
    limit: int = Query(50, ge=1, le=500),
    seat_id: Optional[str] = Query(None),
    decision: Optional[str] = Query(None),
    _user: dict = Depends(get_current_user),
) -> dict:
    q: dict = {}
    if seat_id:
        q["seat_id"] = seat_id
    if decision:
        q["decision"] = decision
    rows = await db[PARADOX_V2_EVALUATIONS].find(q, {"_id": 0}).sort(
        "ts", -1,
    ).to_list(limit)
    return {"items": rows, "count": len(rows)}


# ─── /v2/brains ───────────────────────────────────────────────────────


@router.get("/brains")
async def get_brains(_user: dict = Depends(get_current_user)) -> dict:
    rows = await db[PARADOX_V2_BRAIN_REGISTRY].find({}, {"_id": 0}).to_list(50)
    return {"items": rows, "count": len(rows)}


# ─── /v2/governor-rules ───────────────────────────────────────────────


@router.get("/governor-rules")
async def get_governor_rules(_user: dict = Depends(get_current_user)) -> dict:
    rows = await db[PARADOX_V2_GOVERNOR_RULES].find({}, {"_id": 0}).to_list(50)
    return {"items": rows, "count": len(rows)}
