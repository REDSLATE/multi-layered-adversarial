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

# ── 2026-07-12 doctrine step 5.b: fresh-input tolerance ──
# When all engaged brains carry `source_bar_close_at` on their stances
# (v2 fingerprint path), we require the max-min spread across those
# timestamps to stay within this tolerance before advancing to
# consensus_long/short. 900s = 15 min covers a 5-minute-bar universe
# comfortably (four brains reading four consecutive bar closes within
# a 15-min band is honest agreement; wider than that means the brains
# are looking at different market epochs and their agreement is stale).
# Override via env `CONSENSUS_FRESH_INPUT_TOLERANCE_SEC`.
import os as _os  # noqa: E402
CONSENSUS_FRESH_INPUT_TOLERANCE_SEC = int(
    _os.environ.get("CONSENSUS_FRESH_INPUT_TOLERANCE_SEC", "900")
)


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
# 2026-07-12 (P6a): Pydantic models extracted to positions_models.py
# to reduce this module's line count. Re-exported here so external
# imports of `shared.positions.StanceIn` etc. keep working.
from shared.positions_models import (  # noqa: E402
    BrainT,
    StanceT,
    DirectionT,
    CALL_MODE_AUTO,
    CALL_MODE_MANUAL,
    VALID_CALL_MODES,
    ProposeIn,
    StanceIn,
    OperatorStanceIn,
    ExecutorCallIn,
    RejectIn,
)


# ──────────────────────── helpers ────────────────────────

async def _executor_seat() -> Optional[str]:
    """Returns the brain currently holding the executor seat, or None
    if vacated."""
    r = await get_roster()
    return r["assignments"].get("executor")


async def _stance_summary(position_id: str) -> dict:  # noqa: D401
    # 2026-07-12 (P6a): moved to positions_quorum.py. This shim
    # preserves the internal call sites without a rename cascade.
    from shared.positions_quorum import _stance_summary as _impl
    return await _impl(position_id)


async def _compute_quorum(stances_by_brain: dict[str, dict],
                          stances_by_seat: dict[str, dict],
                          roster_assignments: dict[str, Optional[str]]) -> dict:  # noqa: D401
    # 2026-07-12 (P6a): moved to positions_quorum.py.
    from shared.positions_quorum import _compute_quorum as _impl
    return await _impl(stances_by_brain, stances_by_seat, roster_assignments)


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
        {"position_id": position_id}, {"_id": 0}, max_time_ms=3000,
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
    ).max_time_ms(8000).to_list(limit)
    hydrated = [await _hydrate(r) for r in rows]
    return {"items": hydrated, "count": len(hydrated)}


@router.get("/shared/positions/{position_id}")
async def get_position(
    position_id: str,
    _user: dict = Depends(get_current_user),
):
    doc = await db[SHARED_POSITIONS].find_one(
        {"position_id": position_id}, {"_id": 0}, max_time_ms=3000,
    )
    if not doc:
        raise HTTPException(status_code=404, detail="position not found")
    out = await _hydrate(doc)
    out["audit"] = await db[SHARED_POSITION_AUDIT].find(
        {"position_id": position_id}, {"_id": 0},
    ).sort("ts", -1).max_time_ms(8000).to_list(50)
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
    ).max_time_ms(8000).to_list(limit)
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
        {"position_id": position_id}, {"_id": 0}, max_time_ms=3000,
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
        source_bar_close_at=body.source_bar_close_at,
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
        {"position_id": position_id}, {"_id": 0}, max_time_ms=3000,
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
        source_bar_close_at=body.source_bar_close_at,
    )


# ── 2026-02-11 (P6a-finish): state-machine helpers extracted ──
# `_stance_doc`, `_current_seat_and_epoch`, `_maybe_auto_advance`,
# and `_persist_stance` moved to `positions_state.py`. Re-exported
# below so `from shared.positions import _persist_stance` etc.
# keep working (the runtime + operator stance endpoints in this
# file call these directly, and several tests import them by name).
from shared.positions_state import (  # noqa: E402
    _stance_doc,
    _current_seat_and_epoch,
    _maybe_auto_advance,
    _persist_stance,
)




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
        {"position_id": position_id}, {"_id": 0}, max_time_ms=3000,
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
        {"position_id": position_id}, {"_id": 0}, max_time_ms=3000,
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
        {"position_id": position_id}, {"_id": 0}, max_time_ms=3000,
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
        {"position_id": position_id}, {"_id": 0}, max_time_ms=3000,
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
    ).sort("updated_at", 1).max_time_ms(8000).to_list(100)
    return {
        "items": rows,
        "count": len(rows),
        "stale_after_hours": STALE_AFTER_HOURS,
    }
