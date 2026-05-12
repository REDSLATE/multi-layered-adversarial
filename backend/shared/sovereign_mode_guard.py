"""Sovereign sidecar mode guard + promotion bridge.

Doctrine:
    Each of the four brains can run as a deterministic sovereign sidecar
    (`runtime_patch_kit/sovereign/`) — local-state, replayable, isolated.
    The brain talks to Mission Control via two endpoints:

      1. POST /api/runtime-discussion/positions/{id}/stance
         (existing) — the brain's vote on an open position.

      2. POST /api/runtime-discussion/sovereign/contribution
         (this module) — periodic snapshot of the brain's internal
         state (weights, learning rate, recent outcomes, optional
         confidence delta).

    Two modes the brain may declare:

      * `DTD` — Deterministic Training Data. The brain is reading
        historical bars / labeled replay; weight updates are expected
        and accepted by MC.
      * `PRD` — Production. The brain is reading live market data; MC
        REJECTS any field that would imply a training step
        (`training_signal=true`, `weight_delta != 0`). Snapshots are
        still accepted — operators can see the brain's posture — but
        the brain may not learn from live data without a deliberate
        DTD replay.

    Confidence deltas (the brain's request to nudge its own confidence
    based on recent performance) are HARD-CAPPED at ±0.25. The seat
    policy of whatever seat the brain currently holds is snapshotted on
    every contribution so the audit trail records "Camaro as Executor
    asked for a +0.18 confidence bump" not just "Camaro did something."

    `live_trading_enabled` MUST be False in every payload. Schema
    rejects True. This is the third defense; the brain core itself
    defaults False, the sidecar runner re-asserts False, and the API
    refuses True. Three locks for one door.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

from auth import get_current_user
from db import db
from namespaces import (
    DISCUSSION_PARTICIPANTS,
    SOVEREIGN_AUDIT_LOG,
    SOVEREIGN_STATE,
    SOVEREIGN_STATE_HISTORY,
)
from runtime_auth import verify_runtime_token
from shared.roster import get_roster
from shared.seat_policy import snapshot as seat_snapshot


# ──────────────────────── doctrine constants ────────────────────────

MODE_DTD = "DTD"
MODE_PRD = "PRD"
VALID_MODES = frozenset({MODE_DTD, MODE_PRD})

# Confidence deltas are hard-capped. A brain that wants more than this
# in one step is misbehaving — likely a runaway training loop.
CONFIDENCE_DELTA_CAP = 0.25

# Pulled from the core; we re-assert here so the API is the single
# trust boundary. The brain core uses [-3, +3] and lr ≤ 0.5; we accept
# anything in those bounds.
WEIGHT_MAX_ABS = 3.0
LEARNING_RATE_MAX = 0.5

# Max items the brain may ship in a single contribution. Keeps payload
# bounded; brains rotate older outcomes into local state and only ship
# the recent tail.
MAX_FEATURES = 16
MAX_RECENT_OUTCOMES = 50


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ──────────────────────── models ────────────────────────

class SovereignOutcome(BaseModel):
    """One resolved decision in the brain's recent history."""
    symbol: str = Field(..., min_length=1, max_length=32)
    action: Literal["BUY", "SELL", "HOLD"]
    confidence: float = Field(..., ge=0.0, le=1.0)
    # +1 win, -1 loss, 0 unresolved/flat
    outcome: Literal[-1, 0, 1]
    resolved_at: Optional[str] = Field(default=None, max_length=64)
    notional: float = Field(default=0.0, ge=0.0)


class SovereignContribution(BaseModel):
    """Periodic snapshot the brain sidecar POSTs to MC.

    Stored as the brain's current sovereign-state doc (latest wins) AND
    appended to the history collection so we can replay drift later."""

    mode: Literal["DTD", "PRD"]
    # Always False. Schema rejects True even if the brain mistakenly
    # flipped its local flag. Triple-locked door.
    live_trading_enabled: Literal[False] = False
    weights: dict[str, float] = Field(default_factory=dict)
    learning_rate: float = Field(default=0.0, ge=0.0, le=LEARNING_RATE_MAX)
    # Optional confidence-delta request — the brain saying "based on my
    # recent win/loss tape, I want to nudge my baseline confidence by X."
    # Bounded at ±CONFIDENCE_DELTA_CAP server-side.
    confidence_delta: float = Field(default=0.0)
    delta_reason: str = Field(default="", max_length=256)
    # `True` when the brain is shipping a contribution that would update
    # weights at MC's snapshot (only legal in DTD mode). PRD-mode
    # contributions MUST set this False.
    training_signal: bool = False
    recent_outcomes: list[SovereignOutcome] = Field(
        default_factory=list, max_length=MAX_RECENT_OUTCOMES,
    )
    notes: str = Field(default="", max_length=2048)

    @field_validator("weights")
    @classmethod
    def _weights_bounded(cls, v: dict) -> dict[str, float]:
        if len(v) > MAX_FEATURES:
            raise ValueError(f"weights may have at most {MAX_FEATURES} features")
        out: dict[str, float] = {}
        for k, raw in v.items():
            if not isinstance(k, str) or not k:
                raise ValueError("weight keys must be non-empty strings")
            if len(k) > 32:
                raise ValueError(f"weight key too long: {k[:32]}...")
            try:
                f = float(raw)
            except (TypeError, ValueError) as e:
                raise ValueError(f"weight[{k!r}] must be a number") from e
            if not (-WEIGHT_MAX_ABS <= f <= WEIGHT_MAX_ABS):
                raise ValueError(
                    f"weight[{k!r}]={f} must be in [-{WEIGHT_MAX_ABS}, {WEIGHT_MAX_ABS}]"
                )
            out[k] = f
        return out

    @field_validator("confidence_delta")
    @classmethod
    def _delta_finite(cls, v: float) -> float:
        # Server-side cap is enforced separately so we can still log
        # the original request, but reject hostile +∞ here.
        if v != v or v in (float("inf"), float("-inf")):
            raise ValueError("confidence_delta must be a finite number")
        return float(v)


# ──────────────────────── guard ────────────────────────

def assert_contribution_safe(c: SovereignContribution) -> dict:
    """Apply the doctrinal mode guard and return a guard report.

    Raises HTTPException on violations. Returns the bounded contribution
    fields the caller should persist (clamped delta, etc.).
    """
    # Defense-in-depth: schema already pinned `live_trading_enabled=False`
    # but check again so an upstream type-coercion bug can't sneak True
    # through. Mode must also be a known one.
    if c.live_trading_enabled is True:
        raise HTTPException(
            status_code=422,
            detail="live_trading_enabled must be False (observation-only doctrine)",
        )
    if c.mode not in VALID_MODES:
        raise HTTPException(
            status_code=422,
            detail=f"mode must be one of {sorted(VALID_MODES)}",
        )

    # PRD mode: the brain is reading live market data. Learning against
    # live data is precisely how brains get poisoned (look-ahead bias,
    # overfitting to one regime, etc.). MC refuses training signals
    # from PRD-mode brains. The brain may still SNAPSHOT its state.
    if c.mode == MODE_PRD and c.training_signal:
        raise HTTPException(
            status_code=422,
            detail=(
                "PRD-mode brains may not ship training_signal=True. "
                "Switch to DTD mode for replay training; PRD is "
                "observation-only at the brain layer."
            ),
        )

    # Clamp the delta. We do NOT raise — clamping is the contract;
    # raising would force the brain to track our cap in its own code.
    raw_delta = c.confidence_delta
    bounded_delta = max(-CONFIDENCE_DELTA_CAP, min(CONFIDENCE_DELTA_CAP, raw_delta))
    delta_clamped = bounded_delta != raw_delta

    return {
        "bounded_confidence_delta": bounded_delta,
        "delta_was_clamped": delta_clamped,
        "raw_confidence_delta": raw_delta,
    }


# ──────────────────────── seat-policy snapshot ────────────────────────

async def _current_seat_and_epoch(brain: str) -> tuple[Optional[str], Optional[int]]:
    try:
        roster = await get_roster()
    except Exception:  # noqa: BLE001
        return None, None
    seat_epoch = roster.get("seat_epoch")
    for role, occupant in roster["assignments"].items():
        if occupant == brain:
            return role, seat_epoch
    return None, seat_epoch


# ──────────────────────── persistence ────────────────────────

async def _persist_snapshot(brain: str, c: SovereignContribution,
                            guard: dict) -> dict:
    seat, seat_epoch = await _current_seat_and_epoch(brain)
    policy = seat_snapshot(seat)
    now = _now_iso()

    doc = {
        "brain": brain,
        "mode": c.mode,
        "live_trading_enabled": False,    # canonicalized
        "weights": dict(c.weights),
        "learning_rate": c.learning_rate,
        "training_signal": c.training_signal,
        # Always the bounded delta — the raw value lives only on the
        # history row so operators can spot brains hammering against the
        # cap.
        "confidence_delta": guard["bounded_confidence_delta"],
        "delta_reason": c.delta_reason,
        "recent_outcomes": [o.model_dump() for o in c.recent_outcomes],
        "notes": c.notes,
        # Seat snapshot — authority record. If the brain is later moved
        # to a different seat, this row still tells us what it was
        # allowed to influence at write time.
        "posted_as": policy["posted_as"],
        "seat_epoch": seat_epoch,
        "may_decide": policy["may_decide"],
        "may_execute": policy["may_execute"],
        "may_override": policy["may_override"],
        "may_veto": policy["may_veto"],
        "updated_at": now,
    }

    # Latest-snapshot collection (one doc per brain).
    await db[SOVEREIGN_STATE].update_one(
        {"brain": brain},
        {"$set": doc, "$setOnInsert": {"first_seen_at": now}},
        upsert=True,
    )

    # Immutable history row — one per contribution. Includes the raw
    # delta + clamp flag so operator can audit clipping.
    history_row = {
        **doc,
        "raw_confidence_delta": guard["raw_confidence_delta"],
        "delta_was_clamped": guard["delta_was_clamped"],
        "received_at": now,
    }
    await db[SOVEREIGN_STATE_HISTORY].insert_one(history_row)

    # Audit log — operator-readable timeline.
    await db[SOVEREIGN_AUDIT_LOG].insert_one({
        "ts": now,
        "brain": brain,
        "action": "contribution",
        "mode": c.mode,
        "training_signal": c.training_signal,
        "delta_was_clamped": guard["delta_was_clamped"],
        "posted_as": policy["posted_as"],
        "seat_epoch": seat_epoch,
    })

    # Re-fetch the canonical doc minus _id for return.
    stored = await db[SOVEREIGN_STATE].find_one(
        {"brain": brain}, {"_id": 0},
    )
    stored["delta_was_clamped"] = guard["delta_was_clamped"]
    stored["raw_confidence_delta"] = guard["raw_confidence_delta"]
    return stored


# ──────────────────────── router ────────────────────────

router = APIRouter(tags=["sovereign"])


@router.post("/runtime-discussion/sovereign/contribution")
async def post_sovereign_contribution(
    body: SovereignContribution,
    runtime: str = Query(..., description="brain posting the contribution"),
    x_runtime_token: str | None = Header(default=None, alias="X-Runtime-Token"),
):
    """Brain sidecar POSTs its current sovereign state to MC.

    Auth: per-runtime ingest token (`X-Runtime-Token` header), same
    scheme as opinions / stances. Returns the canonical stored snapshot
    plus guard report (whether the delta was clamped)."""
    verify_runtime_token(runtime, x_runtime_token or "")
    if runtime not in DISCUSSION_PARTICIPANTS:
        # verify_runtime_token also checks this; double-check for clarity.
        raise HTTPException(
            status_code=400,
            detail=f"runtime must be one of {DISCUSSION_PARTICIPANTS}",
        )

    guard = assert_contribution_safe(body)
    return await _persist_snapshot(runtime, body, guard)


# Operator-facing reads (frontend tile uses these).

@router.get("/admin/sovereign/state")
async def list_sovereign_state(_user: dict = Depends(get_current_user)):
    """List the latest sovereign snapshot for every brain that has
    contributed at least once."""
    rows = await db[SOVEREIGN_STATE].find({}, {"_id": 0}).to_list(32)
    return {"items": rows, "count": len(rows)}


@router.get("/admin/sovereign/state/{brain}")
async def get_sovereign_state(
    brain: str, _user: dict = Depends(get_current_user),
):
    if brain not in DISCUSSION_PARTICIPANTS:
        raise HTTPException(
            status_code=400,
            detail=f"brain must be one of {DISCUSSION_PARTICIPANTS}",
        )
    doc = await db[SOVEREIGN_STATE].find_one({"brain": brain}, {"_id": 0})
    if not doc:
        raise HTTPException(
            status_code=404,
            detail=f"no sovereign state on file for {brain}",
        )
    history = await db[SOVEREIGN_STATE_HISTORY].find(
        {"brain": brain}, {"_id": 0},
    ).sort("received_at", -1).to_list(20)
    doc["history"] = history
    return doc


@router.get("/admin/sovereign/audit")
async def sovereign_audit(
    brain: Optional[str] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    _user: dict = Depends(get_current_user),
):
    q = {"brain": brain} if brain else {}
    rows = await db[SOVEREIGN_AUDIT_LOG].find(q, {"_id": 0}).sort(
        "ts", -1,
    ).to_list(limit)
    return {"items": rows, "count": len(rows)}
