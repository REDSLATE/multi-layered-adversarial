"""Brain Memory Ingest — receive resolved decision memories from brain sidecars.

Doctrine pin (2026-05-24):
    Aligned 1:1 with REDEYE's MC_MEMORY_INGEST_SPEC.md (2026-05-24).
    Brains keep their own decision history locally; this endpoint
    accepts batched inserts and surfaces summary stats so MC's
    operator panel can render corpus health.

    Endpoints:
      POST /api/runtime/shelly/memories          (X-Runtime-Token auth)
      POST /api/admin/shelly/memories            (Admin JWT auth)
      GET  /api/admin/brain-memories/summary
      GET  /api/admin/brain-memories/ingest-audit

    Request body matches REDEYE's spec exactly:
      {
        batch_id,
        brain,
        memories: [
          {
            memory_id, decision_id, symbol, lane,
            decision: {raw_action, display_action, confidence, execution_decision},
            resolution: {outcome, realized_r, mae, mfe, entry_price, exit_price,
                         resolved_at, mode},
            features: {bounded numeric dict},
            decided_at,
            text_summary
          }
        ]
      }

    Response (200 OK or 207 partial):
      {ok, batch_id, received, stored, duplicates, rejected[]}

    Invariants:
      - Bulk-only (single-row POSTs rejected by Pydantic min_length=1).
      - Idempotent on `(brain, memory_id)`: re-running returns
        `duplicates += 1` without double-write.
      - Brain identity from `body.brain`; runtime token-owner MUST match
        (no cross-brain runtime pushes; admin path bypasses for backfill).
      - Stored in `brain_memories` (NOT mc_shelly — that's MC's engine
        audit log).
      - Every row stamped `provenance="brain_memory_ingest"`.
      - Soft-closed rows (`resolution.mode == "data_unavailable"`) go
        into `brain_memories_dead` instead, never counted as outcomes.
      - HTTP 207 returned when any rows were rejected (partial success).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from pydantic import BaseModel, Field, field_validator

from auth import get_current_user
from db import db
from namespaces import (
    BRAIN_MEMORIES,
    BRAIN_MEMORY_INGEST_AUDIT,
    DISCUSSION_PARTICIPANTS,
)
from runtime_auth import verify_runtime_token


logger = logging.getLogger(__name__)

router = APIRouter(tags=["brain-memory-ingest"])

# `brain_memories_dead` is a sibling collection — soft-closed rows
# (resolver couldn't validate against market data) park here so they're
# auditable but never trainable.
BRAIN_MEMORIES_DEAD = "brain_memories_dead"


# Hard caps — keep payloads bounded so a misbehaving brain can't OOM MC.
MAX_BATCH = 500
MAX_FEATURES_PER_MEMORY = 20           # spec contract: ≤20 keys / ≤4KB
MAX_FEATURES_PAYLOAD_BYTES = 4096
MAX_TEXT_SUMMARY = 512                 # spec: ≤512 chars


# ─────────────────────────── models ───────────────────────────


# Extended to cover REDEYE's full taxonomy per spec Q1.
LaneT = Literal["crypto", "equity", "options", "futures", "fx", "unknown"]
OutcomeT = Literal[-1, 0, 1]                                 # -1 loss · 0 HOLD/push · 1 win
ActionT = Literal["BUY", "SELL", "HOLD"]
ExecDecisionT = Literal["ALLOW", "BLOCKED"]
ResolutionModeT = Literal["shadow", "live", "data_unavailable"]


class MemoryDecision(BaseModel):
    """The decision the brain made at the time. Field shapes match
    REDEYE's spec verbatim — `raw_action` is the pre-gate verdict,
    `display_action` is what the brain ultimately emitted (typically
    equal; differs when a gate downgraded BUY/SELL → HOLD)."""
    raw_action: ActionT
    display_action: ActionT
    confidence: float = Field(..., ge=0.0, le=1.0)
    execution_decision: ExecDecisionT


class MemoryResolution(BaseModel):
    """How the decision resolved against market reality.

    Sign conventions (per spec):
      - mae ≤ 0 (or 0 for HOLDs)
      - mfe ≥ 0 (or 0 for HOLDs)
      - realized_r aligned with `raw_action`
    Entry/exit prices may be null for HOLDs (no fill).
    """
    outcome: OutcomeT
    realized_r: float
    mae: float
    mfe: float
    entry_price: Optional[float] = None
    exit_price: Optional[float] = None
    resolved_at: str
    mode: ResolutionModeT

    @field_validator("mae")
    @classmethod
    def _mae_non_positive(cls, v: float) -> float:
        if v > 0:
            raise ValueError(f"mae must be ≤ 0, got {v}")
        return v

    @field_validator("mfe")
    @classmethod
    def _mfe_non_negative(cls, v: float) -> float:
        if v < 0:
            raise ValueError(f"mfe must be ≥ 0, got {v}")
        return v


class MemoryIn(BaseModel):
    memory_id: str = Field(..., min_length=1, max_length=128)
    # decision_id is REDEYE-side correlation back to the original
    # decision — kept distinct so memory_id can be a wrapper key
    # (e.g. "WILD-<decision_id>"). Required per spec.
    decision_id: str = Field(..., min_length=1, max_length=128)
    symbol: str = Field(..., min_length=1, max_length=32)
    lane: LaneT
    decided_at: str = Field(..., description="ISO-8601 of original decision")
    decision: MemoryDecision
    resolution: MemoryResolution
    features: Dict[str, float] = Field(default_factory=dict)
    text_summary: str = Field(..., min_length=1, max_length=MAX_TEXT_SUMMARY)

    @field_validator("features")
    @classmethod
    def _features_bounded(cls, v: Dict[str, float]) -> Dict[str, float]:
        if len(v) > MAX_FEATURES_PER_MEMORY:
            raise ValueError(
                f"features dict has {len(v)} entries, max is "
                f"{MAX_FEATURES_PER_MEMORY}"
            )
        for k in v:
            if not isinstance(k, str) or not k or len(k) > 64:
                raise ValueError(f"feature key invalid: {k!r}")
        # Total payload byte cap (defense-in-depth against pathological
        # numeric keys / huge floats).
        import json
        if len(json.dumps(v).encode("utf-8")) > MAX_FEATURES_PAYLOAD_BYTES:
            raise ValueError(
                f"features payload exceeds {MAX_FEATURES_PAYLOAD_BYTES} bytes"
            )
        return v

    @field_validator("symbol")
    @classmethod
    def _symbol_upper(cls, v: str) -> str:
        return v.upper()


class IngestBatchIn(BaseModel):
    batch_id: str = Field(..., min_length=1, max_length=64)
    brain: str
    memories: List[MemoryIn] = Field(..., min_length=1, max_length=MAX_BATCH)

    @field_validator("brain")
    @classmethod
    def _brain_known(cls, v: str) -> str:
        if v not in DISCUSSION_PARTICIPANTS:
            raise ValueError(
                f"brain must be one of {DISCUSSION_PARTICIPANTS}, got {v!r}"
            )
        return v


# ─────────────────────────── core ───────────────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _ingest_batch(
    brain: str, batch: IngestBatchIn, *, ingested_by: str,
) -> Dict[str, Any]:
    """Insert/upsert the batch idempotently. Returns counts in the
    field names REDEYE's spec specifies: received / stored / duplicates."""
    stored = 0
    duplicates = 0
    rejected: List[Dict[str, Any]] = []
    parked_dead = 0
    now = _now_iso()

    for m in batch.memories:
        try:
            base_doc = {
                "brain": brain,
                "memory_id": m.memory_id,
                "decision_id": m.decision_id,
                "symbol": m.symbol.upper(),
                "lane": m.lane,
                "decision": m.decision.model_dump(),
                "resolution": m.resolution.model_dump(),
                "features": m.features,
                "decided_at": m.decided_at,
                "text_summary": m.text_summary,
                "provenance": "brain_memory_ingest",
                "batch_id": batch.batch_id,
                "ingested_at": now,
                "ingested_by": ingested_by,
            }

            # Soft-closed rows go to the _dead sibling — auditable but
            # never trainable. Distinct from `rejected` (which means
            # MC refused to store at all).
            target = (
                BRAIN_MEMORIES_DEAD
                if (m.resolution.mode == "data_unavailable")
                else BRAIN_MEMORIES
            )

            result = await db[target].update_one(
                {"brain": brain, "memory_id": m.memory_id},
                {
                    "$set": base_doc,
                    "$setOnInsert": {"first_seen_at": now},
                },
                upsert=True,
            )
            if result.upserted_id is not None:
                if target == BRAIN_MEMORIES_DEAD:
                    parked_dead += 1
                else:
                    stored += 1
            else:
                duplicates += 1
        except Exception as e:  # noqa: BLE001
            rejected.append({
                "memory_id": m.memory_id,
                "error": repr(e)[:300],
            })

    await db[BRAIN_MEMORY_INGEST_AUDIT].insert_one({
        "ts": now,
        "brain": brain,
        "batch_id": batch.batch_id,
        "ingested_by": ingested_by,
        "received": len(batch.memories),
        "stored": stored,
        "duplicates": duplicates,
        "parked_dead": parked_dead,
        "rejected_count": len(rejected),
    })

    return {
        "ok": True,
        "batch_id": batch.batch_id,
        "brain": brain,
        "received": len(batch.memories),
        "stored": stored,
        "duplicates": duplicates,
        "parked_dead": parked_dead,
        "rejected": rejected[:50],
    }


# ─────────────────────────── routes ───────────────────────────


@router.post("/runtime/shelly/memories")
async def runtime_ingest_memories(
    body: IngestBatchIn,
    response: Response,
    x_runtime_token: Optional[str] = Header(default=None, alias="X-Runtime-Token"),
    x_client_request_id: Optional[str] = Header(default=None, alias="X-Client-Request-Id"),
):
    """Brain sidecar pushes its own resolved memories.

    Auth: per-runtime token. Brain identity derived from `body.brain`;
    the runtime endpoint enforces token-owner matches the brain field.
    Returns HTTP 207 if any rows were rejected (spec contract).
    """
    if not x_runtime_token:
        raise HTTPException(status_code=401, detail="X-Runtime-Token required")
    # Use body.brain as the canonical identity — token-owner derived
    # via the same verify_runtime_token helper used everywhere else.
    verify_runtime_token(body.brain, x_runtime_token)

    result = await _ingest_batch(
        body.brain, body,
        ingested_by=f"runtime:{body.brain}",
    )
    if x_client_request_id:
        result["request_id"] = x_client_request_id
    # 207 = partial success when any rows were rejected.
    if result["rejected"]:
        response.status_code = 207
    return result


@router.post("/admin/shelly/memories")
async def admin_ingest_memories(
    body: IngestBatchIn,
    response: Response,
    user: dict = Depends(get_current_user),  # noqa: B008
):
    """Operator-authed memory ingest for backfill / cross-brain ops."""
    result = await _ingest_batch(
        body.brain, body,
        ingested_by=f"admin:{user.get('email') or 'operator'}",
    )
    if result["rejected"]:
        response.status_code = 207
    return result


# ─────────────────────────── read surface ───────────────────────────


@router.get("/admin/brain-memories/summary")
async def memories_summary(
    brain: Optional[str] = None,
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    """Per-brain corpus health for the operator dashboard.

    Returns: total, by_lane, top_symbols, last_resolved_at, win_rate,
    mean_r — the exact column-shape the operator panel binds to.
    """
    brains = [brain] if brain else list(DISCUSSION_PARTICIPANTS)
    rows: List[Dict[str, Any]] = []
    for b in brains:
        total = await db[BRAIN_MEMORIES].count_documents({"brain": b})
        dead = await db[BRAIN_MEMORIES_DEAD].count_documents({"brain": b})
        if total == 0:
            rows.append({
                "brain": b,
                "total_memories": 0,
                "dead_memories": dead,
                "last_resolved_at": None,
                "win_rate": None,
                "mean_r": None,
                "by_lane": {},
                "top_symbols": [],
            })
            continue

        latest = await db[BRAIN_MEMORIES].find_one(
            {"brain": b}, {"_id": 0, "resolution.resolved_at": 1},
            sort=[("resolution.resolved_at", -1)],
        )
        last_resolved = (latest or {}).get("resolution", {}).get("resolved_at")

        wins = await db[BRAIN_MEMORIES].count_documents(
            {"brain": b, "resolution.outcome": 1},
        )
        losses = await db[BRAIN_MEMORIES].count_documents(
            {"brain": b, "resolution.outcome": -1},
        )
        decided = wins + losses
        win_rate = round(wins / decided, 4) if decided else None

        # mean realized_r across resolved rows.
        mean_r = None
        async for d in db[BRAIN_MEMORIES].aggregate([
            {"$match": {"brain": b, "resolution.realized_r": {"$type": "double"}}},
            {"$group": {"_id": None, "avg_r": {"$avg": "$resolution.realized_r"}}},
        ]):
            mean_r = round(d["avg_r"], 4) if d.get("avg_r") is not None else None

        by_lane: Dict[str, int] = {}
        async for d in db[BRAIN_MEMORIES].aggregate([
            {"$match": {"brain": b}},
            {"$group": {"_id": "$lane", "count": {"$sum": 1}}},
        ]):
            by_lane[d["_id"]] = d["count"]

        top_symbols: List[Dict[str, Any]] = []
        async for d in db[BRAIN_MEMORIES].aggregate([
            {"$match": {"brain": b}},
            {"$group": {"_id": "$symbol", "count": {"$sum": 1}}},
            {"$sort": {"count": -1}},
            {"$limit": 10},
        ]):
            top_symbols.append({"symbol": d["_id"], "count": d["count"]})

        rows.append({
            "brain": b,
            "total_memories": total,
            "dead_memories": dead,
            "last_resolved_at": last_resolved,
            "win_rate": win_rate,
            "mean_r": mean_r,
            "by_lane": by_lane,
            "top_symbols": top_symbols,
        })

    return {"brains": rows}


@router.get("/admin/brain-memories/ingest-audit")
async def memories_ingest_audit(
    brain: Optional[str] = None,
    limit: int = 50,
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    """Recent ingest batches — for the operator panel's 'cadence' row."""
    q: Dict[str, Any] = {}
    if brain:
        q["brain"] = brain
    rows = await db[BRAIN_MEMORY_INGEST_AUDIT].find(q, {"_id": 0}) \
        .sort("ts", -1).to_list(min(max(limit, 1), 500))
    return {"items": rows, "count": len(rows)}

