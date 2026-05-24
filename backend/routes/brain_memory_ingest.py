"""Brain Memory Ingest — receive resolved decision memories from brain sidecars.

Doctrine pin (2026-05-24):
    Brains keep their own decision history locally (intent → resolution
    → outcome). For the operator panel to render a "what does this brain
    actually know?" column AND for downstream consumers to train against
    the same canonical store, those resolved memories need to live in MC
    too. This module accepts batched inserts and surfaces summary stats.

    Endpoint contract (matches the spec REDEYE drafted, with one minor
    refinement: separate admin vs. runtime auth paths so operators can
    backfill on behalf of a brain that's offline):

      POST /api/runtime/shelly/memories
        Auth: X-Runtime-Token (brain self-pushes its own memories)
      POST /api/admin/shelly/memories
        Auth: Admin JWT (operator pushes on behalf of any brain)

      Body:
        {
          batch_id: "uuid",                       # client-generated
          brain: "redeye",                        # must match token
          memories: [
            {
              memory_id: "<brain-side uuid>",     # idempotency key
              symbol: "BTC/USD",
              lane: "crypto",                     # crypto|equity|options
              decision_ts: "2026-05-01T...",
              decision: {action, confidence, ...},
              resolution: {outcome, realized_r, mfe_pct, mae_pct, resolved_at, ...},
              features: {bounded dict, <= 32 entries},
              evidence_refs: [strings, pointers to brain-side artifacts]
            },
            ...
          ]
        }

      Response:
        {ok, batch_id, brain, accepted, deduped, rejected, errors[]}

    Invariants:
      - Bulk-only (no per-memory endpoint).
      - Batch size capped at MAX_BATCH (default 500).
      - Idempotent on `(brain, memory_id)`: re-running a batch = no
        double-write; deduped count returned for telemetry.
      - Every row tagged `provenance = "brain_memory_ingest"` so
        downstream queries can include/exclude trivially.
      - Stored in `brain_memories` — DISTINCT from `shared_intents`
        and `execution_receipts`. Never pollute the forward-looking
        intent stream with backfill data.
      - Audit row written to `brain_memory_ingest_audit` per batch so
        the operator panel can show ingest cadence.

    What this endpoint does NOT do:
      - Run gates. These are resolved memories, gate replay is a
        separate (future) endpoint.
      - Route to broker. Memories are historical, no execution path.
      - Compute calibration curves. That's REDEYE's calibrator
        endpoint (`/api/ingest/calibrators`).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, Header, HTTPException
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


# Hard caps — keep payloads bounded so a misbehaving brain can't OOM MC.
MAX_BATCH = 500
MAX_FEATURES_PER_MEMORY = 32
MAX_EVIDENCE_REFS = 16


# ─────────────────────────── models ───────────────────────────


LaneT = Literal["crypto", "equity", "options"]
ActionT = Literal["BUY", "SELL", "HOLD"]
OutcomeT = Literal[-1, 0, 1]  # -1 loss, 0 push/abstain, 1 win


class MemoryDecision(BaseModel):
    """The decision the brain made at the time."""
    action: ActionT
    confidence: float = Field(..., ge=0.0, le=1.0)
    # Optional brain-side reasoning hooks. Bounded so brains can't
    # ship arbitrary blobs.
    rationale: Optional[str] = Field(default=None, max_length=2000)


class MemoryResolution(BaseModel):
    """How the decision resolved against market reality."""
    outcome: OutcomeT
    realized_r: Optional[float] = None       # realized R-multiple
    mfe_pct: Optional[float] = None          # max favourable excursion
    mae_pct: Optional[float] = None          # max adverse excursion
    resolved_at: str                          # ISO-8601


class MemoryIn(BaseModel):
    memory_id: str = Field(..., min_length=1, max_length=128)
    symbol: str = Field(..., min_length=1, max_length=32)
    lane: LaneT
    decision_ts: str
    decision: MemoryDecision
    resolution: MemoryResolution
    features: Dict[str, float] = Field(default_factory=dict)
    evidence_refs: List[str] = Field(default_factory=list)

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
        return v

    @field_validator("evidence_refs")
    @classmethod
    def _refs_bounded(cls, v: List[str]) -> List[str]:
        if len(v) > MAX_EVIDENCE_REFS:
            raise ValueError(
                f"evidence_refs has {len(v)} entries, max is "
                f"{MAX_EVIDENCE_REFS}"
            )
        return [r[:256] for r in v if isinstance(r, str)]


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
    """Insert/upsert the batch idempotently. Returns counts."""
    accepted = 0
    deduped = 0
    rejected = 0
    errors: List[Dict[str, Any]] = []
    now = _now_iso()

    for m in batch.memories:
        try:
            # Idempotent upsert keyed on (brain, memory_id).
            doc = {
                "brain": brain,
                "memory_id": m.memory_id,
                "symbol": m.symbol.upper(),
                "lane": m.lane,
                "decision_ts": m.decision_ts,
                "decision": m.decision.model_dump(),
                "resolution": m.resolution.model_dump(),
                "features": m.features,
                "evidence_refs": m.evidence_refs,
                "provenance": "brain_memory_ingest",
                "batch_id": batch.batch_id,
                "ingested_at": now,
                "ingested_by": ingested_by,
            }
            result = await db[BRAIN_MEMORIES].update_one(
                {"brain": brain, "memory_id": m.memory_id},
                {
                    "$set": doc,
                    "$setOnInsert": {"first_seen_at": now},
                },
                upsert=True,
            )
            if result.upserted_id is not None:
                accepted += 1
            else:
                # Update matched an existing row — treat as dedupe.
                deduped += 1
        except Exception as e:  # noqa: BLE001
            rejected += 1
            errors.append({
                "memory_id": m.memory_id,
                "error": repr(e)[:300],
            })

    # Batch-level audit row so the operator panel can render ingest
    # cadence + outcome.
    await db[BRAIN_MEMORY_INGEST_AUDIT].insert_one({
        "ts": now,
        "brain": brain,
        "batch_id": batch.batch_id,
        "ingested_by": ingested_by,
        "n_received": len(batch.memories),
        "accepted": accepted,
        "deduped": deduped,
        "rejected": rejected,
        "errors_count": len(errors),
    })

    return {
        "ok": True,
        "brain": brain,
        "batch_id": batch.batch_id,
        "n_received": len(batch.memories),
        "accepted": accepted,
        "deduped": deduped,
        "rejected": rejected,
        "errors": errors[:20],  # cap error array so response stays bounded
    }


# ─────────────────────────── routes ───────────────────────────


@router.post("/runtime/shelly/memories")
async def runtime_ingest_memories(
    body: IngestBatchIn,
    runtime: str = Header(..., alias="X-Runtime", description="brain identity"),
    x_runtime_token: Optional[str] = Header(default=None, alias="X-Runtime-Token"),
):
    """Brain sidecar pushes its own resolved memories.

    Auth: per-runtime token. The `body.brain` field MUST match the
    runtime; a brain can NOT push memories on behalf of another brain
    (use the admin endpoint for that — operator override).
    """
    if not x_runtime_token:
        raise HTTPException(status_code=401, detail="X-Runtime-Token required")
    verify_runtime_token(runtime, x_runtime_token)

    if body.brain != runtime:
        raise HTTPException(
            status_code=403,
            detail=(
                f"runtime={runtime!r} cannot ingest memories for "
                f"brain={body.brain!r}. Use /api/admin/shelly/memories "
                f"for cross-brain operator backfill."
            ),
        )

    return await _ingest_batch(runtime, body, ingested_by=f"runtime:{runtime}")


@router.post("/admin/shelly/memories")
async def admin_ingest_memories(
    body: IngestBatchIn,
    user: dict = Depends(get_current_user),  # noqa: B008
):
    """Operator-authed memory ingest. Useful for backfilling memories
    from a brain that's offline or for one-shot CLI pushes."""
    return await _ingest_batch(
        body.brain, body, ingested_by=f"admin:{user.get('email') or 'operator'}",
    )


# ─────────────────────────── read surface ───────────────────────────


@router.get("/admin/brain-memories/summary")
async def memories_summary(
    brain: Optional[str] = None,
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    """Per-brain corpus health for the operator dashboard.

    For each brain returns: total memories, last_ingested_at, win rate
    (count of outcome=1 / total resolved), lane breakdown, top symbols
    by sample count. Designed for ~< 100ms render time so the panel
    can poll it on a 30s cadence."""
    brains = [brain] if brain else list(DISCUSSION_PARTICIPANTS)
    rows: List[Dict[str, Any]] = []
    for b in brains:
        total = await db[BRAIN_MEMORIES].count_documents({"brain": b})
        if total == 0:
            rows.append({
                "brain": b,
                "total_memories": 0,
                "last_ingested_at": None,
                "win_rate": None,
                "by_lane": {},
                "top_symbols": [],
            })
            continue

        latest = await db[BRAIN_MEMORIES].find_one(
            {"brain": b}, {"_id": 0, "ingested_at": 1},
            sort=[("ingested_at", -1)],
        )
        wins = await db[BRAIN_MEMORIES].count_documents(
            {"brain": b, "resolution.outcome": 1},
        )
        losses = await db[BRAIN_MEMORIES].count_documents(
            {"brain": b, "resolution.outcome": -1},
        )
        decided = wins + losses
        win_rate = round(wins / decided, 4) if decided else None

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
            "last_ingested_at": latest.get("ingested_at") if latest else None,
            "win_rate": win_rate,
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
