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



# ═════════════════════════════════════════════════════════════════════
# Vote-doctrine layer (2026-02-19, additive — does NOT touch /v2/evaluate)
# ═════════════════════════════════════════════════════════════════════


from datetime import datetime as _dt, timezone as _tz  # noqa: E402
from typing import Any as _Any  # noqa: E402
from shared.brain_vote import (  # noqa: E402
    BrainVote, CalibrationKey, MarketMemoryResult,
)
from governor.disagreement import compute_disagreement  # noqa: E402
from verifier.replay import VerifierReplay, ReplayCase  # noqa: E402
from shared.paradox_v2.vote_doctrine_repo import (  # noqa: E402
    save_brain_vote, list_recent_votes, load_votes_by_ids,
    save_failure_attribution, list_recent_attributions,
)


# ─── /v2/votes — cast & list immutable BrainVotes ────────────────────


class MemoryEvidencePayload(BaseModel):
    similar_count: int = Field(..., ge=0)
    win_rate: float = Field(..., ge=0.0, le=1.0)
    avg_return_bps: float
    worst_drawdown_bps: float
    failure_pattern: Optional[str] = None


class CalibrationKeyPayload(BaseModel):
    regime: str
    conf_bucket: float = Field(..., ge=0.0, le=1.0)


class CastVoteRequest(BaseModel):
    brain: str
    stance: Literal["BUY", "SELL", "HOLD", "ABSTAIN"]
    raw_confidence: float = Field(..., ge=0.0, le=1.0)
    calibrated_confidence: float = Field(..., ge=0.0, le=1.0)
    calibration_key: CalibrationKeyPayload
    memory_evidence: Optional[MemoryEvidencePayload] = None
    negative_knowledge_triggered: bool = False
    reasoning: list[str] = Field(..., min_length=1)
    # Operator context — for read filtering only; the brain layer
    # has no notion of symbol/regime in its BrainVote dataclass.
    symbol: Optional[str] = None
    regime: Optional[str] = None


@router.post("/votes/cast")
async def post_cast_vote(
    body: CastVoteRequest,
    _user: dict = Depends(get_current_user),
) -> dict:
    """Persist a single brain's immutable vote.

    The full BrainVote invariants run at construction time — invalid
    votes raise 422. Trading remains unaffected; this endpoint only
    writes to paradox_v2_brain_votes and never reaches any seat or
    broker."""
    mem = None
    if body.memory_evidence is not None:
        mem = MarketMemoryResult(
            similar_count=body.memory_evidence.similar_count,
            win_rate=body.memory_evidence.win_rate,
            avg_return_bps=body.memory_evidence.avg_return_bps,
            worst_drawdown_bps=body.memory_evidence.worst_drawdown_bps,
            failure_pattern=body.memory_evidence.failure_pattern,
        )
    try:
        vote = BrainVote(
            brain=body.brain,
            stance=body.stance,
            calibrated_confidence=body.calibrated_confidence,
            raw_confidence=body.raw_confidence,
            calibration_key=CalibrationKey(
                regime=body.calibration_key.regime,
                conf_bucket=body.calibration_key.conf_bucket,
            ),
            memory_evidence=mem,
            negative_knowledge_triggered=body.negative_knowledge_triggered,
            reasoning=tuple(body.reasoning),
            timestamp=_dt.now(_tz.utc),
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    vote_id = await save_brain_vote(vote, symbol=body.symbol, regime=body.regime)
    return {"ok": True, "vote_id": vote_id,
            "stance": vote.stance,
            "calibrated_confidence": vote.calibrated_confidence}


@router.get("/votes")
async def get_votes(
    limit: int = Query(50, ge=1, le=500),
    brain: Optional[str] = Query(None),
    symbol: Optional[str] = Query(None),
    _user: dict = Depends(get_current_user),
) -> dict:
    rows = await list_recent_votes(limit=limit, brain=brain, symbol=symbol)
    return {"items": rows, "count": len(rows)}


# ─── /v2/disagreement — compute metrics on a vote bundle ─────────────


class DisagreementRequest(BaseModel):
    vote_ids: list[str] = Field(..., min_length=1)
    regime: str


@router.post("/disagreement")
async def post_disagreement(
    body: DisagreementRequest,
    _user: dict = Depends(get_current_user),
) -> dict:
    """Compute DisagreementMetrics across a set of persisted votes.

    Pure read + pure math — the governor would consume the returned
    metrics to decide size cuts, but this endpoint never executes
    any downstream action."""
    votes = await load_votes_by_ids(body.vote_ids)
    if not votes:
        raise HTTPException(status_code=404, detail="no votes resolved from vote_ids")
    metrics = compute_disagreement(votes, regime=body.regime)
    return {
        "ok": True,
        "vote_count": len(votes),
        "metrics": {
            "entropy": metrics.entropy,
            "outlier_brain": metrics.outlier_brain,
            "outlier_stance": metrics.outlier_stance,
            "regime_mismatch": metrics.regime_mismatch,
            "abstention_rate": metrics.abstention_rate,
            "majority_stance": metrics.majority_stance,
            "majority_confidence": metrics.majority_confidence,
        },
    }


# ─── /v2/replay — verifier failure attribution on a case ─────────────


class ReplayRequest(BaseModel):
    """Operator-supplied replay case. The verifier reads from real
    paradox_v2_evaluations + execution_receipts in production; for
    stand-alone testing this lets you POST a synthetic case."""
    symbol: str
    regime: str
    direction: Literal["BUY", "SELL", "HOLD"]
    notional_usd: float = Field(..., ge=0.0)
    pnl_bps: float
    vote_ids: list[str] = Field(..., min_length=1)
    loss_threshold_bps: int = Field(-50)
    roadguard_decision: Literal["OPEN", "BLOCKED"] = "OPEN"


@router.post("/replay")
async def post_replay(
    body: ReplayRequest,
    _user: dict = Depends(get_current_user),
) -> dict:
    """Run VerifierReplay.analyze() on a case assembled from persisted
    votes. Always persists the resulting FailureReason to
    paradox_v2_failure_attributions."""
    votes = await load_votes_by_ids(body.vote_ids)
    if not votes:
        raise HTTPException(status_code=404, detail="no votes resolved from vote_ids")
    case = ReplayCase(
        timestamp=_dt.now(_tz.utc),
        symbol=body.symbol.upper().strip(),
        regime=body.regime,
        brain_votes={v.brain: v for v in votes},
        governor_output={},
        roadguard_decision=body.roadguard_decision,
        seat_action={"direction": body.direction,
                     "notional_usd": body.notional_usd},
        actual_outcome={"pnl_bps": body.pnl_bps},
    )
    reason = VerifierReplay(loss_threshold_bps=body.loss_threshold_bps).analyze(case)
    attribution_id = await save_failure_attribution(
        reason,
        case_context={
            "symbol": case.symbol, "regime": case.regime,
            "direction": body.direction, "pnl_bps": body.pnl_bps,
            "vote_ids": body.vote_ids,
        },
    )
    return {
        "ok": True,
        "attribution_id": attribution_id,
        "failure_reason": {
            "type": reason.type.value,
            "responsible_brain": reason.responsible_brain,
            "calibration_error": reason.calibration_error,
            "memory_error": reason.memory_error,
            "negative_knowledge_miss": reason.negative_knowledge_miss,
            "explanation": reason.explanation,
        },
    }


@router.get("/attributions")
async def get_attributions(
    limit: int = Query(50, ge=1, le=500),
    responsible_brain: Optional[str] = Query(None),
    _user: dict = Depends(get_current_user),
) -> dict:
    rows = await list_recent_attributions(
        limit=limit, responsible_brain=responsible_brain,
    )
    return {"items": rows, "count": len(rows)}
