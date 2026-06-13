"""Position primitive — the discrete object all 4 brains argue over.

Doctrine (2026-02-11):
    A Position is "we are debating long/short on SYMBOL right now."
    It is created by the operator (or a brain, in a future iteration);
    every brain stamps a stance (long / short / abstain) with confidence
    + notes; the brain in the executor seat (per Roster — default Alpha)
    makes the final call. Phase 1 is discussion-only — no order
    placement, no broker side-effects.

State machine:
    proposed       — created, no stances yet
    discussing     — at least one stance posted
    consensus_long — executor called LONG (state advance, audit-logged)
    consensus_short— executor called SHORT
    rejected       — executor walked away (no trade thesis)
    stale          — auto-expires after STALE_AFTER_HOURS with no activity

Doctrine guards:
    - `may_execute` stays schema-pinned False on every endpoint.
    - The executor's "call" is a state-machine advance, NOT a trade.
    - Brain stance ingestion uses the existing X-Runtime-Token header
      (per-brain). Operator stance ingestion uses the JWT path. Brains
      cannot impersonate each other.
    - Every state change is audit-logged with actor + before/after.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

from auth import get_current_user
from db import db
from namespaces import (
    DISCUSSION_PARTICIPANTS,
    SHARED_POSITION_AUDIT,
    SHARED_POSITION_STANCES,
    SHARED_POSITIONS,
)
from runtime_auth import verify_runtime_token
from shared.roster import get_roster
from shared.seat_policy import SEAT_POLICY, required_seats, snapshot as seat_snapshot


STATE_PROPOSED = "proposed"
STATE_DISCUSSING = "discussing"
STATE_CONSENSUS_LONG = "consensus_long"
STATE_CONSENSUS_SHORT = "consensus_short"
STATE_REJECTED = "rejected"
STATE_STALE = "stale"

OPEN_STATES = frozenset({STATE_PROPOSED, STATE_DISCUSSING})
TERMINAL_STATES = frozenset({
    STATE_CONSENSUS_LONG, STATE_CONSENSUS_SHORT, STATE_REJECTED, STATE_STALE,
})

STANCE_LONG = "long"
STANCE_SHORT = "short"
STANCE_ABSTAIN = "abstain"
VALID_STANCES = frozenset({STANCE_LONG, STANCE_SHORT, STANCE_ABSTAIN})

STALE_AFTER_HOURS = 48


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _audit(action: str, actor: str, position_id: str, payload: dict) -> None:
    await db[SHARED_POSITION_AUDIT].insert_one({
        "ts": _now_iso(),
        "action": action,
        "actor": actor,
        "position_id": position_id,
        "payload": payload,
    })


# ──────────────────────── models ────────────────────────

BrainT = Literal["alpha", "camaro", "chevelle", "redeye"]
StanceT = Literal["long", "short", "abstain"]
DirectionT = Literal["long", "short"]


CALL_MODE_AUTO = "auto"
CALL_MODE_MANUAL = "manual"
VALID_CALL_MODES = frozenset({CALL_MODE_AUTO, CALL_MODE_MANUAL})


class ProposeIn(BaseModel):
    symbol: str = Field(..., min_length=1, max_length=32)
    regime_tag: Optional[str] = Field(default=None, max_length=48)
    thesis: str = Field("", max_length=2048)
    proposed_by: str = Field(..., description="brain name or 'operator'")
    call_mode: Literal["auto", "manual"] = Field(
        default="manual",
        description=(
            "auto: the executor seat's long/short stance immediately "
            "advances state. manual: operator clicks CALL LONG / CALL "
            "SHORT to advance."
        ),
    )

    @field_validator("symbol")
    @classmethod
    def _norm_symbol(cls, v: str) -> str:
        return v.strip().upper()

    @field_validator("proposed_by")
    @classmethod
    def _proposed_by_check(cls, v: str) -> str:
        v = v.strip().lower()
        if v != "operator" and v not in DISCUSSION_PARTICIPANTS:
            raise ValueError(
                f"proposed_by must be 'operator' or one of {DISCUSSION_PARTICIPANTS}"
            )
        return v


class StanceIn(BaseModel):
    stance: StanceT
    confidence: float = Field(0.5, ge=0.0, le=1.0)
    notes: str = Field("", max_length=2048)
    # Memory provenance (optional — brains opt in once they emit it).
    # When a brain reports which memory artefacts shaped this stance,
    # we record them so future audits can trace memory poisoning, stale
    # priors, or reinforcement loops. Empty list is acceptable.
    memory_sources: list[str] = Field(default_factory=list, max_length=32)
    # Confidence origin breakdown (optional). Brains that can decompose
    # their confidence into named components (model, memory,
    # contradiction_penalty, regime_alignment, …) report them here.
    # Validated below to keep keys/values bounded.
    confidence_origin: dict[str, float] = Field(default_factory=dict)

    @field_validator("memory_sources")
    @classmethod
    def _norm_sources(cls, v: list[str]) -> list[str]:
        out: list[str] = []
        for s in v:
            if not isinstance(s, str):
                raise ValueError("memory_sources must be strings")
            t = s.strip()
            if not t:
                continue
            if len(t) > 128:
                raise ValueError(f"memory_source too long: {t[:32]}...")
            out.append(t)
        return out

    @field_validator("confidence_origin")
    @classmethod
    def _norm_confidence_origin(cls, v: dict) -> dict[str, float]:
        if len(v) > 12:
            raise ValueError("confidence_origin can have at most 12 components")
        out: dict[str, float] = {}
        for k, val in v.items():
            if not isinstance(k, str) or not k:
                raise ValueError("confidence_origin keys must be non-empty strings")
            if len(k) > 64:
                raise ValueError(f"confidence_origin key too long: {k[:32]}...")
            try:
                f = float(val)
            except (TypeError, ValueError) as e:
                raise ValueError(
                    f"confidence_origin[{k!r}] must be a number"
                ) from e
            if not (-1.0 <= f <= 1.0):
                raise ValueError(
                    f"confidence_origin[{k!r}]={f} must be in [-1, 1]"
                )
            out[k.strip()] = f
        return out


class OperatorStanceIn(StanceIn):
    """Operator posting a stance on behalf of a brain (or themselves)."""
    brain: BrainT


class ExecutorCallIn(BaseModel):
    """Operator advances the position via the executor seat's decision.
    direction='long' → consensus_long; 'short' → consensus_short;
    a separate /reject endpoint handles walk-away.
    """
    direction: DirectionT
    notes: str = Field("", max_length=2048)


class RejectIn(BaseModel):
    notes: str = Field("", max_length=2048)


# ──────────────────────── helpers ────────────────────────

async def _executor_seat() -> Optional[str]:
    """Returns the brain currently holding the executor seat, or None
    if vacated."""
    r = await get_roster()
    return r["assignments"].get("executor")


async def _stance_summary(position_id: str) -> dict:
    """Aggregate per-brain stance into a compact summary for list views."""
    rows = await db[SHARED_POSITION_STANCES].find(
        {"position_id": position_id}, {"_id": 0},
    ).sort("posted_at", 1).to_list(64)
    by_brain: dict[str, dict] = {}
    for r in rows:
        # Latest wins (a brain can refine its stance — last one stands).
        by_brain[r["brain"]] = r
    counts = {"long": 0, "short": 0, "abstain": 0}
    for stance in by_brain.values():
        if stance["stance"] in counts:
            counts[stance["stance"]] += 1
    return {
        "stances_by_brain": by_brain,
        "stance_counts": counts,
        "brains_engaged": len(by_brain),
    }


async def _compute_quorum(stances_by_brain: dict[str, dict],
                          stances_by_seat: dict[str, dict],
                          roster_assignments: dict[str, Optional[str]]) -> dict:
    """Quorum awareness — POSITION model (Doctrine, 2026-05-30).

    A required seat is "engaged" iff the brain CURRENTLY holding that
    seat has authored a stance on this position. Authority lives in
    the seat; when the seat rotates, the new holder must re-speak.
    A stance written by the previous holder no longer satisfies the
    seat's quorum — because authority moved with the seat.

    Prior implementation read `posted_as` (seat-at-write-time) and
    counted any historical stance under that seat as engagement,
    even after rotation. That allowed Alpha to take the strategist
    seat while Camaro's old strategist stance silently held quorum
    on his behalf — which is brain-coupling masquerading as
    "history". Same fix family as the executor_seat_check
    position-model relaxation (2026-05-28).

    Computes:
      - seats_engaged: required seats whose CURRENT holder has stanced
      - seats_required: list of seats marked seat_required=True
      - seats_missing: required seats whose current holder is silent
        (either no stance from current holder, or seat is vacant)
      - vacant_required_seats: required seats that have no brain assigned
        (worse than silent — there's literally no one to ask)
      - adversarial_blindness: auditor seat is required and unstamped
        (2026-05-27 — opponent merged into auditor; this flag now
        triggers on auditor silence)
      - governance_blindness: governor seat is required and unstamped
      - degraded: any required seat is unstamped or vacant
    """
    req = list(required_seats())
    engaged: list[str] = []
    missing: list[str] = []
    vacant_required: list[str] = []
    for seat in req:
        current_holder = roster_assignments.get(seat)
        if not current_holder:
            vacant_required.append(seat)
            missing.append(seat)
            continue
        # Position-model engagement: current holder must have stanced.
        if current_holder in stances_by_brain:
            engaged.append(seat)
        else:
            missing.append(seat)
    return {
        "seats_engaged": engaged,
        "seats_required": req,
        "seats_missing": missing,
        "vacant_required_seats": vacant_required,
        # 2026-05-27 doctrine merge: opponent merged into auditor. The
        # auditor now carries BOTH pre-trade-contrary AND post-trade
        # review. Adversarial blindness now triggers when the auditor
        # is silent on a position.
        "adversarial_blindness": "auditor" in missing,
        "governance_blindness": "governor" in missing,
        "degraded": len(missing) > 0,
    }


async def _hydrate(doc: dict) -> dict:
    summary = await _stance_summary(doc["position_id"])
    roster = {}
    try:
        roster = await get_roster()
    except Exception:  # noqa: BLE001
        roster = {"assignments": {}}
    # Build seat → stance map FOR DISPLAY ONLY. This shows the operator
    # "what stance was last written under each seat" regardless of who
    # currently holds it — useful historical context for the UI. Quorum
    # itself uses `stances_by_brain` + current roster to enforce the
    # position-model engagement check (see `_compute_quorum`).
    stances_by_seat: dict[str, dict] = {}
    for stance in summary["stances_by_brain"].values():
        seat = stance.get("posted_as")
        if seat:
            stances_by_seat[seat] = stance
    quorum = await _compute_quorum(
        summary["stances_by_brain"],
        stances_by_seat,
        roster.get("assignments") or {},
    )
    return {
        **doc,
        **summary,
        "stances_by_seat": stances_by_seat,
        "executor_seat": await _executor_seat(),
        "quorum": quorum,
    }


async def _advance_state_if_needed(position_id: str) -> Optional[dict]:
    """Auto-bump proposed → discussing on first stance."""
    doc = await db[SHARED_POSITIONS].find_one(
        {"position_id": position_id}, {"_id": 0},
    )
    if not doc:
        return None
    if doc["state"] == STATE_PROPOSED:
        await db[SHARED_POSITIONS].update_one(
            {"position_id": position_id},
            {"$set": {
                "state": STATE_DISCUSSING,
                "updated_at": _now_iso(),
            }},
        )
        doc["state"] = STATE_DISCUSSING
    return doc


# ──────────────────────── router ────────────────────────

router = APIRouter(tags=["positions"])


@router.post("/shared/positions")
async def propose_position(
    body: ProposeIn,
    user: dict = Depends(get_current_user),
):
    """Operator (or a brain via a future runtime endpoint) opens a new
    position for discussion. Idempotent on (symbol, day) is NOT enforced —
    operator can open multiple positions on the same symbol intentionally
    (different theses, different time-frames)."""
    now = _now_iso()
    doc = {
        "position_id": str(uuid.uuid4()),
        "symbol": body.symbol,
        "regime_tag": body.regime_tag,
        "thesis": body.thesis,
        "proposed_by": body.proposed_by,
        "state": STATE_PROPOSED,
        "direction": None,
        "executor_call_by": None,
        "executor_call_at": None,
        "call_mode": body.call_mode,    # auto | manual
        "created_at": now,
        "updated_at": now,
        "created_by_operator": user.get("email") or "operator",
    }
    await db[SHARED_POSITIONS].insert_one(doc)
    await _audit("propose", body.proposed_by, doc["position_id"], {
        "symbol": body.symbol, "regime_tag": body.regime_tag,
        "call_mode": body.call_mode,
    })
    out = {k: v for k, v in doc.items() if k != "_id"}
    return await _hydrate(out)


@router.get("/shared/positions")
async def list_positions(
    state: Optional[str] = Query(None),
    symbol: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=500),
    _user: dict = Depends(get_current_user),
):
    q: dict = {}
    if state == "open":
        q["state"] = {"$in": list(OPEN_STATES)}
    elif state == "terminal":
        q["state"] = {"$in": list(TERMINAL_STATES)}
    elif state:
        q["state"] = state
    if symbol:
        q["symbol"] = symbol.upper()
    rows = await db[SHARED_POSITIONS].find(q, {"_id": 0}).sort(
        "updated_at", -1,
    ).to_list(limit)
    hydrated = [await _hydrate(r) for r in rows]
    return {"items": hydrated, "count": len(hydrated)}


@router.get("/shared/positions/{position_id}")
async def get_position(
    position_id: str,
    _user: dict = Depends(get_current_user),
):
    doc = await db[SHARED_POSITIONS].find_one(
        {"position_id": position_id}, {"_id": 0},
    )
    if not doc:
        raise HTTPException(status_code=404, detail="position not found")
    out = await _hydrate(doc)
    out["audit"] = await db[SHARED_POSITION_AUDIT].find(
        {"position_id": position_id}, {"_id": 0},
    ).sort("ts", -1).to_list(50)
    return out


# ── runtime discovery: list open positions for brain-side polling ──

@router.get("/runtime-discussion/positions")
async def runtime_list_positions(
    runtime: str = Query(..., description="brain identity making the discovery call"),
    status: Optional[str] = Query(
        "open",
        description="open | terminal | (any specific state) — defaults to open",
    ),
    symbol: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=500),
    x_runtime_token: str | None = Header(default=None, alias="X-Runtime-Token"),
):
    """Brain-facing position discovery (2026-05-24).

    Returns the same shape as the operator endpoint `/shared/positions`
    but authenticated via the per-runtime ingest token rather than the
    admin JWT — so brain sidecars can poll position state on their own
    cadence and stamp stances against the returned `position_id`s.

    Doctrine pin: this is READ-ONLY. Brains discover; they don't open
    or close positions through this surface. Position lifecycle stays
    with MC's gate chain.

    Returned rows include `position_id`, `symbol`, `side`, `lane`,
    `state`, `opened_at`, `updated_at`, `stances_by_brain` (so a brain
    can see what it has ALREADY stamped and avoid double-posting), and
    `stance_counts`."""
    verify_runtime_token(runtime, x_runtime_token or "")

    q: dict = {}
    if status == "open":
        q["state"] = {"$in": list(OPEN_STATES)}
    elif status == "terminal":
        q["state"] = {"$in": list(TERMINAL_STATES)}
    elif status:
        q["state"] = status
    if symbol:
        q["symbol"] = symbol.upper()

    rows = await db[SHARED_POSITIONS].find(q, {"_id": 0}).sort(
        "updated_at", -1,
    ).to_list(limit)
    hydrated = [await _hydrate(r) for r in rows]
    return {
        "runtime": runtime,
        "items": hydrated,
        "count": len(hydrated),
        "doctrine_note": (
            "Read-only discovery. POST stance updates back to "
            "/runtime-discussion/positions/{position_id}/stance using "
            "the same X-Runtime-Token. Vocabulary: "
            "stance ∈ {long, short, abstain}; confidence in [0,1]."
        ),
    }


# ── stance posting: operator path (JWT) ──

@router.post("/admin/positions/{position_id}/stance")
async def operator_post_stance(
    position_id: str,
    body: OperatorStanceIn,
    user: dict = Depends(get_current_user),
):
    """Operator stamps a stance on behalf of a brain (or to override what
    a brain wrote). Used when a brain's sidecar isn't running but the
    operator wants the position to reflect that brain's posture."""
    doc = await db[SHARED_POSITIONS].find_one(
        {"position_id": position_id}, {"_id": 0},
    )
    if not doc:
        raise HTTPException(status_code=404, detail="position not found")
    if doc["state"] in TERMINAL_STATES:
        raise HTTPException(
            status_code=409,
            detail=f"position is {doc['state']}; stance closed",
        )

    actor = user.get("email") or "operator"
    return await _persist_stance(
        position_id=position_id, brain=body.brain, stance=body.stance,
        confidence=body.confidence, notes=body.notes,
        posted_via="operator", actor=actor,
        memory_sources=body.memory_sources,
        confidence_origin=body.confidence_origin,
    )


# ── stance posting: brain path (X-Runtime-Token) ──

@router.post("/runtime-discussion/positions/{position_id}/stance")
async def runtime_post_stance(
    position_id: str,
    body: StanceIn,
    runtime: str = Query(..., description="brain posting the stance"),
    x_runtime_token: str | None = Header(default=None, alias="X-Runtime-Token"),
):
    """Brain sidecar stamps its own stance. Auth uses the per-runtime
    ingest token (same scheme as opinions / heartbeats)."""
    verify_runtime_token(runtime, x_runtime_token or "")

    doc = await db[SHARED_POSITIONS].find_one(
        {"position_id": position_id}, {"_id": 0},
    )
    if not doc:
        raise HTTPException(status_code=404, detail="position not found")
    if doc["state"] in TERMINAL_STATES:
        raise HTTPException(
            status_code=409,
            detail=f"position is {doc['state']}; stance closed",
        )

    return await _persist_stance(
        position_id=position_id, brain=runtime, stance=body.stance,
        confidence=body.confidence, notes=body.notes,
        posted_via="runtime", actor=runtime,
        memory_sources=body.memory_sources,
        confidence_origin=body.confidence_origin,
    )


def _stance_doc(
    *, position_id: str, brain: str, stance: str, confidence: float,
    notes: str, seat: Optional[str], seat_epoch: Optional[int],
    policy: dict, posted_via: str, actor: str, now_iso: str,
    memory_sources: list[str], confidence_origin: dict[str, float],
) -> dict:
    """Assemble the stance document with full seat-policy snapshot
    AND memory-provenance fields."""
    return {
        "stance_id": str(uuid.uuid4()),
        "position_id": position_id,
        "brain": brain,
        "stance": stance,
        "confidence": float(confidence),
        "notes": notes,
        # Seat policy snapshot — this is the authority record. If the
        # brain later changes seats, this row STILL reflects what the
        # rules were at write time.
        "posted_as": policy["posted_as"],
        "seat_epoch": seat_epoch,
        "may_decide": policy["may_decide"],
        "may_execute": policy["may_execute"],
        # `may_override` removed from doctrine on 2026-02-19 — see
        # `shared/seat_policy.py` for the 4-seat merge rationale.
        "may_veto": policy["may_veto"],
        # Memory provenance — opt-in by the brain sidecar. Empty arrays
        # are perfectly valid and indicate the brain doesn't (yet)
        # report provenance. Future "memory poisoning" audits will join
        # on these fields.
        "memory_sources": list(memory_sources),
        "confidence_origin": dict(confidence_origin),
        "posted_via": posted_via,
        "posted_at": now_iso,
        "actor": actor,
    }


async def _current_seat_and_epoch(brain: str) -> tuple[Optional[str], Optional[int]]:
    """Resolve which seat the brain currently holds + the live seat_epoch.
    Best-effort: roster lookup failures resolve to (None, None) so callers
    can still ingest with the safest-default policy snapshot."""
    try:
        roster = await get_roster()
    except Exception:  # noqa: BLE001
        return None, None
    seat_epoch = roster.get("seat_epoch")
    for role, occupant in roster["assignments"].items():
        if occupant == brain:
            return role, seat_epoch
    return None, seat_epoch


async def _maybe_auto_advance(
    *, position_id: str, brain: str, stance: str, policy: dict,
    seat_epoch: Optional[int], now_iso: str,
) -> None:
    """If the position is in auto call_mode AND the brain holds the
    executor seat AND the stance is long/short, advance position state.
    Logs `executor_call_auto` so it's distinguishable from operator calls."""
    doc = await db[SHARED_POSITIONS].find_one(
        {"position_id": position_id}, {"_id": 0},
    )
    if not doc:
        return
    if doc.get("call_mode") != CALL_MODE_AUTO:
        return
    if doc["state"] not in OPEN_STATES:
        return
    if not policy["may_execute"]:
        return
    # 2026-02-19 doctrine refresh: the executor seat now ships in TWO
    # flavours (equity executor=alpha, crypto executor=redeye). Each
    # has a lane-scoped authority. A crypto-seated brain must NOT
    # auto-advance an equity position and vice versa.
    seat = policy.get("posted_as")
    if seat:
        from shared.seat_policy import seat_may_execute_lane
        from shared.regime_keys import _looks_like_crypto
        position_lane = (
            "crypto" if _looks_like_crypto(doc.get("symbol") or "") else "equity"
        )
        if not seat_may_execute_lane(seat, position_lane):
            return
    if stance not in (STANCE_LONG, STANCE_SHORT):
        return

    new_state = (
        STATE_CONSENSUS_LONG if stance == STANCE_LONG
        else STATE_CONSENSUS_SHORT
    )
    await db[SHARED_POSITIONS].update_one(
        {"position_id": position_id},
        {"$set": {
            "state": new_state,
            "direction": stance,
            "executor_call_by": brain,
            "executor_call_at": now_iso,
            "executor_call_notes": f"auto-advanced from executor seat ({brain})",
            "executor_call_recorded_by": "auto",
            "executor_call_seat_epoch": seat_epoch,
            "updated_at": now_iso,
        }},
    )
    await _audit("executor_call_auto", brain, position_id, {
        "executor": brain,
        "direction": stance,
        "before_state": doc["state"],
        "after_state": new_state,
        "trigger": "auto_mode_executor_stance",
        "seat_epoch": seat_epoch,
    })


async def _persist_stance(
    *, position_id: str, brain: str, stance: str,
    confidence: float, notes: str, posted_via: str, actor: str,
    memory_sources: list[str] | None = None,
    confidence_origin: dict[str, float] | None = None,
) -> dict:
    if stance not in VALID_STANCES:
        raise HTTPException(
            status_code=422,
            detail=f"stance must be one of {sorted(VALID_STANCES)}",
        )
    now = _now_iso()
    seat, seat_epoch = await _current_seat_and_epoch(brain)
    policy = seat_snapshot(seat)

    await db[SHARED_POSITION_STANCES].insert_one(_stance_doc(
        position_id=position_id, brain=brain, stance=stance,
        confidence=confidence, notes=notes,
        seat=seat, seat_epoch=seat_epoch, policy=policy,
        posted_via=posted_via, actor=actor, now_iso=now,
        memory_sources=memory_sources or [],
        confidence_origin=confidence_origin or {},
    ))
    await db[SHARED_POSITIONS].update_one(
        {"position_id": position_id},
        {"$set": {"updated_at": now}},
    )
    await _advance_state_if_needed(position_id)
    await _audit("stance", actor, position_id, {
        "brain": brain, "stance": stance, "confidence": confidence,
        "posted_as": policy["posted_as"],
        "seat_epoch": seat_epoch,
        "may_execute": policy["may_execute"],
        "posted_via": posted_via,
    })
    await _maybe_auto_advance(
        position_id=position_id, brain=brain, stance=stance,
        policy=policy, seat_epoch=seat_epoch, now_iso=now,
    )

    doc = await db[SHARED_POSITIONS].find_one(
        {"position_id": position_id}, {"_id": 0},
    )
    return await _hydrate(doc)


# ── executor call (operator advances state) ──

@router.post("/admin/positions/{position_id}/executor-call")
async def executor_call(
    position_id: str,
    body: ExecutorCallIn,
    user: dict = Depends(get_current_user),
):
    """Operator records the executor seat's call (long/short).
    Doctrine: this is a state-machine advance, NOT a trade. Order
    placement is gated by the broker exec-toggle, which lives on a
    separate path and stays default-off until Phase 2."""
    doc = await db[SHARED_POSITIONS].find_one(
        {"position_id": position_id}, {"_id": 0},
    )
    if not doc:
        raise HTTPException(status_code=404, detail="position not found")
    if doc["state"] in TERMINAL_STATES:
        raise HTTPException(
            status_code=409,
            detail=f"position already {doc['state']}",
        )

    executor = await _executor_seat()
    if not executor:
        raise HTTPException(
            status_code=400,
            detail="no brain currently holds the executor seat — assign one on /api/admin/roster first",
        )

    new_state = (
        STATE_CONSENSUS_LONG if body.direction == "long"
        else STATE_CONSENSUS_SHORT
    )
    now = _now_iso()
    actor = user.get("email") or "operator"
    await db[SHARED_POSITIONS].update_one(
        {"position_id": position_id},
        {"$set": {
            "state": new_state,
            "direction": body.direction,
            "executor_call_by": executor,
            "executor_call_at": now,
            "executor_call_notes": body.notes,
            "executor_call_recorded_by": actor,
            "updated_at": now,
        }},
    )
    await _audit("executor_call", actor, position_id, {
        "executor": executor,
        "direction": body.direction,
        "before_state": doc["state"],
        "after_state": new_state,
    })
    refreshed = await db[SHARED_POSITIONS].find_one(
        {"position_id": position_id}, {"_id": 0},
    )
    return await _hydrate(refreshed)


@router.post("/admin/positions/{position_id}/reject")
async def reject_position(
    position_id: str,
    body: RejectIn,
    user: dict = Depends(get_current_user),
):
    """Walk away — no trade thesis. Records and audits."""
    doc = await db[SHARED_POSITIONS].find_one(
        {"position_id": position_id}, {"_id": 0},
    )
    if not doc:
        raise HTTPException(status_code=404, detail="position not found")
    if doc["state"] in TERMINAL_STATES:
        raise HTTPException(
            status_code=409,
            detail=f"position already {doc['state']}",
        )
    now = _now_iso()
    actor = user.get("email") or "operator"
    await db[SHARED_POSITIONS].update_one(
        {"position_id": position_id},
        {"$set": {
            "state": STATE_REJECTED,
            "executor_call_notes": body.notes,
            "executor_call_recorded_by": actor,
            "updated_at": now,
        }},
    )
    await _audit("reject", actor, position_id, {
        "before_state": doc["state"], "after_state": STATE_REJECTED,
        "notes": body.notes,
    })
    refreshed = await db[SHARED_POSITIONS].find_one(
        {"position_id": position_id}, {"_id": 0},
    )
    return await _hydrate(refreshed)


# ── stale sweep (read-side, returns "would-be-stale" without mutating) ──

@router.get("/shared/positions/stale-sweep")
async def stale_sweep_preview(_user: dict = Depends(get_current_user)):
    """Show positions that have not been touched in STALE_AFTER_HOURS but
    are still open. Operator can mark them stale via /reject (with notes)
    or just leave them — auto-marking belongs in a background job we'll
    add later."""
    cutoff = (
        datetime.now(timezone.utc) - timedelta(hours=STALE_AFTER_HOURS)
    ).isoformat()
    rows = await db[SHARED_POSITIONS].find(
        {"state": {"$in": list(OPEN_STATES)}, "updated_at": {"$lt": cutoff}},
        {"_id": 0},
    ).sort("updated_at", 1).to_list(100)
    return {
        "items": rows,
        "count": len(rows),
        "stale_after_hours": STALE_AFTER_HOURS,
    }
