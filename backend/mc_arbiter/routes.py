"""MC Arbiter — HTTP surface.

Endpoints (all mounted under `/api/mc/arbiter/`):

    POST /opinion                 brain submits a ModelOpinion
    POST /arbitrate/{seat_key}    close a seat and pick a winner
    GET  /seat/{seat_key}         read the seat doc (opinions + decision)
    GET  /state                   runtime mode + counts
    POST /runtime-mode            operator flips DISARMED ↔ LIVE
    POST /grader/run              manual grader tick (for admin/debug)

Auth: all endpoints require the standard admin session. Brains
authenticate as operators for now — Phase 2 adds a runtime-token
lane so a brain can POST /opinion without operator privilege.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, validator

from db import db
from mc_arbiter.arbiter import (
    MC_SEATS,
    arbitrate,
    get_runtime_mode,
    set_runtime_mode,
    submit_opinion,
)
from mc_arbiter.grader import grade_pending_opinions
from mc_arbiter.models import Direction, ModelOpinion, RuntimeMode
from mc_arbiter.seat_key import build_seat_key
from auth import get_current_user

logger = logging.getLogger("mc_arbiter.routes")
router = APIRouter(prefix="/mc/arbiter", tags=["mc-arbiter"])


# ── Request bodies ───────────────────────────────────────────────

class OpinionIn(BaseModel):
    """POST /opinion payload.

    `seat_key` is optional — if omitted, the arbiter derives it
    from `lane` + `symbol` + now(UTC). Explicit `seat_key` wins
    (deterministic tests, replay scenarios)."""
    brain: str
    lane: str
    symbol: str
    direction: str
    edge: float = Field(ge=0.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    regime_fit: float = Field(ge=0.0, le=1.0)
    urgency: float = Field(ge=0.0, le=1.0)
    price_at_signal: float = Field(gt=0.0)
    ts: str
    seat_key: Optional[str] = None
    entry_hint: Optional[float] = None
    stop_hint: Optional[float] = None
    rationale: str = ""

    @validator("direction")
    def _direction_valid(cls, v):
        try:
            Direction(v)
        except ValueError:
            raise ValueError(
                f"direction must be one of {[d.value for d in Direction]}"
            )
        return v

    @validator("brain")
    def _brain_nonempty(cls, v):
        if not v or not v.strip():
            raise ValueError("brain is required")
        return v.strip().lower()

    @validator("lane")
    def _lane_valid(cls, v):
        v = (v or "").strip().lower()
        if v not in {"equity", "crypto"}:
            raise ValueError("lane must be 'equity' or 'crypto'")
        return v

    @validator("symbol")
    def _symbol_nonempty(cls, v):
        if not v or not v.strip():
            raise ValueError("symbol is required")
        return v.strip().upper()


class RuntimeModeIn(BaseModel):
    mode: str

    @validator("mode")
    def _mode_valid(cls, v):
        try:
            RuntimeMode(v)
        except ValueError:
            raise ValueError(
                f"mode must be one of {[m.value for m in RuntimeMode]}"
            )
        return v


# ── Routes ───────────────────────────────────────────────────────

@router.post("/opinion")
async def post_opinion(
    body: OpinionIn,
    user: dict = Depends(get_current_user),
):
    """Ingest one brain's opinion. Returns a compact receipt."""
    seat_key = body.seat_key or build_seat_key(body.lane, body.symbol)
    opinion = ModelOpinion(
        brain=body.brain,
        seat_key=seat_key,
        direction=Direction(body.direction),
        edge=body.edge,
        confidence=body.confidence,
        regime_fit=body.regime_fit,
        urgency=body.urgency,
        price_at_signal=body.price_at_signal,
        ts=body.ts,
        entry_hint=body.entry_hint,
        stop_hint=body.stop_hint,
        rationale=body.rationale,
    )
    receipt = await submit_opinion(opinion)
    return receipt


@router.post("/arbitrate/{seat_key:path}")
async def post_arbitrate(
    seat_key: str,
    user: dict = Depends(get_current_user),
):
    """Force-close a seat and run the arbitration loop. Runtime
    mode is read from the persisted arbiter state — operator flips
    that via `POST /runtime-mode`, not per-request."""
    mode = await get_runtime_mode()
    decision = await arbitrate(seat_key, runtime_mode=mode)
    return decision


@router.get("/seat/{seat_key:path}")
async def get_seat(
    seat_key: str,
    user: dict = Depends(get_current_user),
):
    """All rows for a seat_key (one row per brain that opined),
    plus whatever decision was stamped on the winning row."""
    docs = await (
        db[MC_SEATS]
        .find({"seat_key": seat_key}, {"_id": 0})
        .max_time_ms(2500)
        .to_list(50)
    )
    if not docs:
        raise HTTPException(status_code=404, detail="seat not found")
    decision = None
    for d in docs:
        if d.get("decision"):
            decision = d["decision"]
            break
    return {
        "seat_key": seat_key,
        "opinions": docs,
        "decision": decision,
        "count": len(docs),
    }


@router.get("/state")
async def get_state(user: dict = Depends(get_current_user)):
    """Runtime mode + recent activity counters. Cheap read."""
    mode = await get_runtime_mode()
    # Bound the counts — a growing mc_seats collection should never
    # stall this dashboard endpoint. See iter-25b Atlas-timeout work.
    try:
        seats_last_hour = await db[MC_SEATS].count_documents(
            {},
            maxTimeMS=1500,
        )
    except Exception:  # noqa: BLE001
        seats_last_hour = None
    return {
        "runtime_mode": mode.value,
        "seats_total": seats_last_hour,
        "doctrine": "one arbiter · one intent per seat · no shadow",
    }


@router.post("/runtime-mode")
async def post_runtime_mode(
    body: RuntimeModeIn,
    user: dict = Depends(get_current_user),
):
    """Flip DISARMED ↔ LIVE. Audit trail records the actor."""
    actor = str(user.get("email") or user.get("sub") or "unknown")
    return await set_runtime_mode(RuntimeMode(body.mode), actor=actor)


@router.post("/grader/run")
async def post_grader_run(user: dict = Depends(get_current_user)):
    """Manual grader tick. For admin/debug — Phase 2 wires this
    into the background worker loop for automatic 60s cadence."""
    return await grade_pending_opinions()
