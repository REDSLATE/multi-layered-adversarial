"""Paradox v2 API surface — `/api/v2/*`.

Stand-alone deployment (2026-02-19): operator-driven endpoints only.
Not wired into the live intent pipeline. After ≥50 manual evaluations
prove the seat-policy concept holds, flip the wire in intents.py.
"""
from __future__ import annotations

from typing import Optional, Literal
from datetime import datetime, timezone
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Query
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
    # 2026-02-20: max_auditor_objections removed — was declared but
    # never enforced anywhere. Auditor objections are advisory per
    # operator doctrine.
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


# ─── /v2/promote-all-seats ────────────────────────────────────────────


class PromoteAllSeatsRequest(BaseModel):
    """Bulk-promote every seat in `PARADOX_V2_SEAT_POLICY` to a target
    autonomy mode in one call. Designed as the operator's "release the
    brakes" master switch — the system was shipping seats in `observe`
    mode by default (Paradox v2 doctrine), which was the systemic
    cause of "rejecting every intent all day" symptoms: every seat
    passed every gate, then died at the final autonomy_mode check.

    `reason` is required (audit trail). `target_mode` defaults to
    `auto_execute` but can be set to `observe` for an emergency-brake
    inverse if a cascade event is observed.
    """
    target_mode: Literal["observe", "shadow", "toehold", "auto_execute"] = "auto_execute"
    reason: str = Field(
        ..., min_length=4,
        description="Audit-trail reason. e.g. 'operator promote all to live after doctrine alignment'",
    )


@router.post("/promote-all-seats")
async def post_promote_all_seats(
    body: PromoteAllSeatsRequest,
    user: dict = Depends(get_current_user),
) -> dict:
    """Master promote — flip every seat's `autonomy_mode` to
    `target_mode` in one operator action. Idempotent: seats already
    at `target_mode` are left untouched (no spurious promotion-log
    entries).

    Returns a per-seat report:
        {
          "ok": true,
          "target_mode": "auto_execute",
          "promoted": [{seat_id, from_mode, to_mode}, ...],
          "unchanged": [seat_id, ...],
          "total": <int>,
        }
    """
    now = _now()
    actor = user.get("email") or "operator"
    promoted: list[dict] = []
    unchanged: list[str] = []

    seats = await db[PARADOX_V2_SEAT_POLICY].find({}, {"_id": 0}).to_list(length=None)
    if not seats:
        raise HTTPException(
            status_code=409,
            detail="no seats in PARADOX_V2_SEAT_POLICY — run POST /api/v2/seed first",
        )

    for seat in seats:
        seat_id = seat["seat_id"]
        from_mode = seat.get("autonomy_mode")
        if from_mode == body.target_mode:
            unchanged.append(seat_id)
            continue
        await db[PARADOX_V2_SEAT_POLICY].update_one(
            {"seat_id": seat_id},
            {"$set": {
                "autonomy_mode": body.target_mode,
                "updated_at": now,
                "updated_by": actor,
            }},
        )
        await db[PARADOX_V2_PROMOTION_LOG].insert_one({
            "promotion_id": str(uuid.uuid4()),
            "seat_id": seat_id,
            "from_mode": from_mode,
            "to_mode": body.target_mode,
            "reason": body.reason,
            "triggered_by": actor,
            "metrics_snapshot": {"bulk_master_promote": True},
            "ts": now,
        })
        promoted.append({
            "seat_id": seat_id,
            "from_mode": from_mode,
            "to_mode": body.target_mode,
        })

    return {
        "ok": True,
        "target_mode": body.target_mode,
        "promoted": promoted,
        "unchanged": unchanged,
        "total": len(seats),
    }


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
    """Persist a single brain's immutable vote (operator/JWT auth).

    The full BrainVote invariants run at construction time — invalid
    votes raise 422. Trading remains unaffected; this endpoint only
    writes to paradox_v2_brain_votes and never reaches any seat or
    broker."""
    return await _cast_vote_impl(body)


@router.post("/votes/runtime-cast")
async def post_runtime_cast_vote(
    body: CastVoteRequest,
    x_runtime_token: Optional[str] = Header(default=None, alias="X-Runtime-Token"),
) -> dict:
    """Brain-runner-friendly variant of /votes/cast.

    Same invariants; uses the per-brain `X-Runtime-Token` header
    instead of operator JWT so the in-process brain runners can
    fire-and-forget without a user session. The runtime-token must
    match the brain id named in the body."""
    if not x_runtime_token:
        raise HTTPException(status_code=401, detail="X-Runtime-Token required")
    from runtime_auth import verify_runtime_token
    try:
        verify_runtime_token(body.brain, x_runtime_token)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=401, detail=f"token verify failed: {e}")
    return await _cast_vote_impl(body)


async def _cast_vote_impl(body: "CastVoteRequest") -> dict:
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


# ─── /v2/votes/emit — server-side calibration (Paradox v2 wire-up) ──


class EmitVoteRequest(BaseModel):
    """Brain ships RAW signals. MC calibrates server-side using its
    persisted history and applies negative-knowledge before persisting."""
    brain: Literal["camino", "barracuda", "hellcat", "gto"]
    stance: Literal["BUY", "SELL", "HOLD"]
    raw_confidence: float = Field(..., ge=0.0, le=1.0)
    symbol: str
    lane: Literal["equity", "crypto"]
    regime: str
    reasoning: list[str] = Field(..., min_length=1)
    setup_hash: Optional[str] = Field(
        None,
        description=(
            "stable hash of the brain's setup for negative-knowledge lookup. "
            "If omitted, MC composes '{symbol}:{regime}:{stance}'."
        ),
    )


@router.post("/votes/emit")
async def post_emit_vote(
    body: EmitVoteRequest,
    x_runtime_token: Optional[str] = Header(default=None, alias="X-Runtime-Token"),
) -> dict:
    """HTTP route: verify token then delegate. In-process callers use
    `submit_vote_in_process` below to skip auth."""
    if not x_runtime_token:
        raise HTTPException(status_code=401, detail="X-Runtime-Token required")
    from runtime_auth import verify_runtime_token
    try:
        verify_runtime_token(body.brain, x_runtime_token)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=401, detail=f"token verify failed: {e}")
    return await _post_emit_vote_impl(body)


async def submit_vote_in_process(body: EmitVoteRequest) -> dict:
    """Direct in-process entrypoint for the brain runner. No auth."""
    return await _post_emit_vote_impl(body)


async def _post_emit_vote_impl(body: EmitVoteRequest) -> dict:
    """Server-side calibrate + check + persist. Same body for HTTP
    and in-process callers; the wrapper above does the auth."""
    # Lazy-hydrate per-brain stores (reuses the verifier-loop caches).
    from shared.paradox_v2.verifier_loop import _get_calibrator, _get_negative_knowledge
    cal = await _get_calibrator(body.brain)
    nk = await _get_negative_knowledge(body.brain)

    setup_hash = body.setup_hash or f"{body.symbol.upper()}:{body.regime}:{body.stance}"
    abstained, nk_reason = nk.check(setup_hash, regime=body.regime)
    path = "calibrated"
    if abstained:
        path = "abstained"
        vote = BrainVote.abstain(
            brain=body.brain,
            reason=nk_reason or f"negative_pattern:{setup_hash}",
            calibration_key=CalibrationKey(
                regime=body.regime,
                conf_bucket=round(body.raw_confidence, 1),
            ),
            raw_confidence=body.raw_confidence,
        )
    else:
        calibrated, key = cal.calibrate(body.raw_confidence, regime=body.regime)
        try:
            vote = BrainVote(
                brain=body.brain,
                stance=body.stance,
                calibrated_confidence=calibrated,
                raw_confidence=body.raw_confidence,
                calibration_key=key,
                memory_evidence=None,
                negative_knowledge_triggered=False,
                reasoning=tuple(body.reasoning),
                timestamp=_dt.now(_tz.utc),
            )
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))

    vote_id = await save_brain_vote(vote, symbol=body.symbol, regime=body.regime)
    return {
        "ok": True,
        "vote_id": vote_id,
        "path": path,
        "stance": vote.stance,
        "raw_confidence": vote.raw_confidence,
        "calibrated_confidence": vote.calibrated_confidence,
        "negative_knowledge_triggered": vote.negative_knowledge_triggered,
        "setup_hash": setup_hash,
    }


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



# ═════════════════════════════════════════════════════════════════════
# Phase 2 vote escalation routes (additive — no live trading hook yet)
# ═════════════════════════════════════════════════════════════════════


from shared.paradox_v2 import vote_session as _vs  # noqa: E402


class OpenVoteSessionRequest(BaseModel):
    intent_id: Optional[str] = None
    symbol: str
    lane: Literal["equity", "crypto"]
    triggered_by: str = Field(..., description="'auditor_veto' | 'governor_vote_required' | operator email")
    reason: str = Field(..., min_length=4)
    excluded_brain: Optional[str] = Field(None, description="brain that auditored; cannot re-vote")
    window_seconds: int = Field(180, ge=30, le=900)
    quorum: int = Field(2, ge=1, le=4)


@router.post("/vote-sessions/open")
async def post_open_vote_session(
    body: OpenVoteSessionRequest,
    _user: dict = Depends(get_current_user),
) -> dict:
    try:
        doc = await _vs.open_session(
            intent_id=body.intent_id,
            symbol=body.symbol, lane=body.lane,
            triggered_by=body.triggered_by, reason=body.reason,
            excluded_brain=body.excluded_brain,
            window_seconds=body.window_seconds, quorum=body.quorum,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return {"ok": True, "session": doc}


class CastBallotRequest(BaseModel):
    brain: Literal["camino", "barracuda", "hellcat", "gto"]
    vote: Literal["BUY_UP", "SELL_DOWN", "HOLD", "ABSTAIN"]
    reason: str = Field(..., min_length=1)


@router.post("/vote-sessions/{session_id}/vote")
async def post_cast_ballot(
    session_id: str,
    body: CastBallotRequest,
    _user: dict = Depends(get_current_user),
) -> dict:
    try:
        doc = await _vs.cast_vote(
            session_id, brain=body.brain,
            vote=body.vote, reason=body.reason,
        )
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return {"ok": True, "session": doc}


@router.post("/vote-sessions/{session_id}/resolve")
async def post_resolve_vote_session(
    session_id: str,
    force: bool = Query(False, description="resolve even if window not yet expired"),
    _user: dict = Depends(get_current_user),
) -> dict:
    try:
        doc = await _vs.resolve(session_id, force=force)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"ok": True, "session": doc}


@router.get("/vote-sessions")
async def get_vote_sessions(
    limit: int = Query(50, ge=1, le=500),
    status: Optional[Literal["OPEN", "CLOSED"]] = Query(None),
    _user: dict = Depends(get_current_user),
) -> dict:
    from namespaces import PARADOX_V2_VOTE_SESSIONS
    q: dict = {}
    if status:
        q["status"] = status
    rows = await db[PARADOX_V2_VOTE_SESSIONS].find(q, {"_id": 0}).sort(
        "opened_at", -1,
    ).to_list(limit)
    return {"items": rows, "count": len(rows)}


@router.post("/vote-sessions/sweep")
async def post_sweep(_user: dict = Depends(get_current_user)) -> dict:
    """Manually trigger the expired-session sweeper. Normally runs in
    the background — exposed here for operator override and tests."""
    return await _vs.sweep_expired()


# ═════════════════════════════════════════════════════════════════════
# Verifier loop control + manual pass
# ═════════════════════════════════════════════════════════════════════


from shared.paradox_v2 import verifier_loop as _vl  # noqa: E402


@router.post("/verifier/run-once")
async def post_verifier_run_once(
    lookback_min: int = Query(60, ge=1, le=1440),
    _user: dict = Depends(get_current_user),
) -> dict:
    """Run one verifier pass synchronously. Reads recent
    execution_receipts, grades any without attributions, persists
    failure attributions and updates calibration/negative-knowledge
    stores in Mongo. Trading impact: zero (read-only on receipts)."""
    return await _vl.run_one_pass(lookback_min=lookback_min)



@router.post("/cache/reset")
async def post_cache_reset(_user: dict = Depends(get_current_user)) -> dict:
    """Bust the in-memory per-brain calibration + negative-knowledge
    caches so the next /v2/votes/emit call re-hydrates from Mongo.
    Use after a direct DB mutation (verifier output bypassing the
    in-memory learn path)."""
    from shared.paradox_v2.verifier_loop import invalidate_caches
    invalidate_caches()
    return {"ok": True, "ts": _dt.now(_tz.utc).isoformat()}


# ─── /v2/seats/pilot-readiness ─────────────────────────────────────────
#
# Operator-driven promotion gating (no auto-promotion — per operator
# directive 2026-02-19, the verifier does NOT promote pilot seats on
# its own). This endpoint surfaces decision-quality stats per seat so
# the operator can decide when to manually patch autonomy_mode.
#
# Readiness threshold: 25 observe-mode evaluations on the current
# autonomy_mode. (Operator-set; tunable via env if the floor moves.)

import os as _os
PILOT_PROMOTION_MIN_EVALS = int(_os.environ.get("PARADOX_V2_PILOT_PROMOTION_MIN_EVALS", "25"))


_PROMOTION_LADDER = ("observe", "shadow", "toehold", "auto_execute")


def _next_mode(current: str) -> Optional[str]:
    try:
        i = _PROMOTION_LADDER.index(current)
    except ValueError:
        return None
    if i + 1 >= len(_PROMOTION_LADDER):
        return None
    return _PROMOTION_LADDER[i + 1]


@router.get("/seats/pilot-readiness")
async def get_pilot_readiness(_user: dict = Depends(get_current_user)) -> dict:
    """Per-seat decision-quality stats and a promotability flag.

    Counts only evaluations in the seat's CURRENT autonomy_mode window
    — promoting a seat resets the clock. The verifier promotion log
    gives us the window-start timestamp.

    Returns:
      readiness: [
        { seat_id, instrument_type, current_mode, next_mode,
          window_started_at, eval_count, blocked_count,
          rejected_seat_count, rejected_roadguard_count,
          executed_count, pending_vote_count, avg_confidence,
          promotable (bool), threshold (int) }
      ]
      threshold: 25
    """
    seats = await db[PARADOX_V2_SEAT_POLICY].find({}, {"_id": 0}).to_list(50)
    out: list[dict] = []
    for seat in seats:
        seat_id = seat["seat_id"]
        current_mode = seat.get("autonomy_mode", "observe")
        # Window-start: last promotion log entry to this mode, else seat seed time.
        last_promo = await db[PARADOX_V2_PROMOTION_LOG].find_one(
            {"seat_id": seat_id, "to_mode": current_mode},
            {"_id": 0}, sort=[("ts", -1)],
        )
        window_start = (last_promo or {}).get("ts") or seat.get("updated_at") or "1970-01-01T00:00:00+00:00"

        cursor = db[PARADOX_V2_EVALUATIONS].find(
            {"seat_id": seat_id, "ts": {"$gte": window_start}}, {"_id": 0},
        )
        evals = await cursor.to_list(10_000)

        counts: dict[str, int] = {}
        conf_sum, conf_n = 0.0, 0
        for e in evals:
            d = e.get("decision", "UNKNOWN")
            counts[d] = counts.get(d, 0) + 1
            c = (e.get("opinion") or {}).get("confidence")
            if isinstance(c, (int, float)):
                conf_sum += float(c)
                conf_n += 1

        total = len(evals)
        nxt = _next_mode(current_mode)
        promotable = (
            current_mode in ("observe", "shadow", "toehold")
            and total >= PILOT_PROMOTION_MIN_EVALS
            and (counts.get("REJECTED_ROADGUARD", 0) == 0)
        )

        out.append({
            "seat_id": seat_id,
            "instrument_type": seat.get("instrument_type"),
            "current_mode": current_mode,
            "next_mode": nxt,
            "window_started_at": window_start,
            "eval_count": total,
            "blocked_count": counts.get("BLOCKED", 0),
            "rejected_seat_count": counts.get("REJECTED_SEAT", 0),
            "rejected_roadguard_count": counts.get("REJECTED_ROADGUARD", 0),
            "executed_count": counts.get("EXECUTED", 0),
            "pending_vote_count": counts.get("PENDING_VOTE", 0),
            "avg_confidence": round(conf_sum / conf_n, 3) if conf_n else None,
            "promotable": promotable,
            "threshold": PILOT_PROMOTION_MIN_EVALS,
        })

    out.sort(key=lambda r: (0 if r["promotable"] else 1, -r["eval_count"]))
    return {"readiness": out, "threshold": PILOT_PROMOTION_MIN_EVALS, "ladder": list(_PROMOTION_LADDER)}


# ─── /v2/council/live ──────────────────────────────────────────────────
#
# Council Chamber — the operator's real-time view of what each brain is
# saying right now. One row per brain, latest BrainVote, projected with
# enough context to read at a glance: who, what stance, on what symbol,
# in what regime, how confident, when.

# Display map for the Council Chamber tile. Source of truth: seed.py
# CANONICAL_BRAINS. Hardcoded here to avoid a DB round-trip per render.
_COUNCIL_DISPLAY_ORDER = ("camino", "barracuda", "hellcat", "gto")
_COUNCIL_DISPLAY_NAMES = {
    "camino":    "Camino",
    "barracuda":   "Barracuda",
    "hellcat": "Hellcat",
    "gto":   "GTO",
}


@router.get("/council/live")
async def get_council_live(_user: dict = Depends(get_current_user)) -> dict:
    """Latest BrainVote per brain — drives the Council Chamber UI.

    Returns one row per canonical brain (alpha, camaro, chevelle,
    redeye). A brain with no recorded vote yet shows `latest=null` so
    the operator can tell the difference between SILENT and HOLD.
    """
    from namespaces import PARADOX_V2_BRAIN_VOTES

    chamber: list[dict] = []
    for brain_id in _COUNCIL_DISPLAY_ORDER:
        latest = await db[PARADOX_V2_BRAIN_VOTES].find_one(
            {"brain": brain_id}, {"_id": 0}, sort=[("timestamp", -1)],
        )
        chamber.append({
            "brain_id": brain_id,
            "display_name": _COUNCIL_DISPLAY_NAMES[brain_id],
            "latest": latest,  # full vote dict; null if brain has never spoken
        })

    # Quorum vitals — how many of the 4 brains have spoken in the
    # last 10 minutes? Operator wants to know if the council is alive.
    from datetime import timedelta as _td
    cutoff = (_dt.now(_tz.utc) - _td(minutes=10)).isoformat()
    alive_ids: set[str] = set()
    async for row in db[PARADOX_V2_BRAIN_VOTES].find(
        {"timestamp": {"$gte": cutoff}}, {"brain": 1, "_id": 0},
    ):
        if row.get("brain") in _COUNCIL_DISPLAY_NAMES:
            alive_ids.add(row["brain"])

    return {
        "chamber": chamber,
        "quorum": {
            "alive_in_10min": sorted(alive_ids),
            "alive_count": len(alive_ids),
            "expected": 4,
        },
        "ts": _dt.now(_tz.utc).isoformat(),
    }
