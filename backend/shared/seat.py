"""Seat — the ONE module that decides whose opinion fires.

Doctrine (2026-02-27 architectural reduction):

    Market Data → Brain → SEAT → Risk → Broker

The Seat is the single point of decision authority. It is the merger
of every "who-decides" layer that previously sprawled across the
codebase:
    * shared/executor_seat.py        (current holder lookup)
    * shared/auditor_seat.py
    * shared/brain_seats.py
    * shared/seat_policy.py
    * shared/seat_state.py
    * shared/pipeline/seat_policy.py
    * shared/consensus.py / consensus_engine.py
    * shared/pipeline/consensus_pool.py / dissent / evidence
    * shared/council.py
    * shared/legacy_brain_wrappers.py (BUY/SELL/HOLD modifications)

If `Seat.decide(intent)` returns `fire`, the intent goes to Risk.
If it returns `pass`, the intent is logged and dropped — no further
gates, no dry-run, no auto-submit policy.

Storage:
    seat_registry — one doc per (lane, role). _id = "<lane>:<role>".
        { holder: str | None, since: iso, assigned_by: email, reason: str }
        Roles: "executor". Lanes: "equity", "crypto".

The Seat is INTENTIONALLY thin. It does not:
    * modify the brain's action (no wrappers)
    * compute a "doctrine score" (that was diagnostic)
    * run a council vote (that was diagnostic)
    * score dissent or evidence quality (that was diagnostic)
    * apply a tier policy (auto_submit_policy is dead)

What it DOES:
    1. Look up the current executor holder for the intent's lane.
    2. If the intent's brain == holder → `fire`.
    3. Else → `pass` (audited).

There is no second-guessing past this point. The operator's authority
to assign the seat IS the decision authority.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from db import db


_SEAT_COLL = "seat_registry"

Verdict = Literal["fire", "pass"]


@dataclass(frozen=True)
class SeatDecision:
    verdict: Verdict
    holder: Optional[str]
    intent_brain: Optional[str]
    lane: Optional[str]
    reason: str


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _seat_id(lane: str, role: str = "executor") -> str:
    return f"{(lane or '').lower()}:{role}"


async def get_holder(lane: str, role: str = "executor") -> Optional[str]:
    """Current holder of (lane, role), or None if vacant.

    Reads from `seat_registry` first (new home). Falls back to the
    legacy `shared_brain_roster.assignments` to preserve existing
    operator seat assignments during the architectural reduction.
    Once the operator re-assigns via `set_holder`, the new registry
    becomes authoritative for that seat."""
    if not lane:
        return None
    doc = await db[_SEAT_COLL].find_one(
        {"_id": _seat_id(lane, role)}, {"_id": 0, "holder": 1}
    )
    if doc:
        h = doc.get("holder")
        if h:
            return h
    # Legacy fallback: shared_brain_roster.assignments[<lane>_<role>]
    # The legacy convention used "<lane>_<role>" for non-equity lanes
    # (e.g. "crypto_executor") and bare "<role>" for equity. Try both.
    legacy_keys = []
    lane_l = (lane or "").lower()
    role_l = (role or "executor").lower()
    if lane_l == "equity":
        legacy_keys.append(role_l)
    legacy_keys.append(f"{lane_l}_{role_l}")
    roster = await db["shared_brain_roster"].find_one(
        {}, {"_id": 0, "assignments": 1}
    )
    assignments = (roster or {}).get("assignments") or {}
    for k in legacy_keys:
        v = assignments.get(k)
        if v:
            return v
    return None


async def set_holder(
    lane: str,
    holder: Optional[str],
    *,
    role: str = "executor",
    assigned_by: str = "operator",
    reason: str = "",
) -> dict:
    """Assign or clear a seat. Upserts the registry row."""
    now = _now_iso()
    doc = {
        "holder": holder,
        "since": now if holder else None,
        "assigned_by": assigned_by if holder else None,
        "reason": reason,
        "last_changed_at": now,
        "lane": (lane or "").lower(),
        "role": role,
    }
    await db[_SEAT_COLL].update_one(
        {"_id": _seat_id(lane, role)},
        {"$set": doc, "$setOnInsert": {"_id": _seat_id(lane, role)}},
        upsert=True,
    )
    return {"ok": True, **doc, "_id": _seat_id(lane, role)}


async def list_seats() -> dict[str, dict]:
    """Snapshot of every seat row. Used by the Seat tile in the UI."""
    out: dict[str, dict] = {}
    async for row in db[_SEAT_COLL].find({}):
        sid = row.pop("_id", None)
        if sid:
            out[sid] = row
    return out


async def decide(intent: dict[str, Any]) -> SeatDecision:
    """The decision. Returns `fire` or `pass`. Nothing else.

    `intent` is a `shared_intents` row. Required keys:
        action: BUY | SELL | SHORT | COVER | HOLD
        lane:   "equity" | "crypto"
        stack:  brain name ("camino" | "barracuda" | "hellcat" | "gto")
        symbol: ticker
    """
    action = (intent.get("action") or "").upper()
    lane = (intent.get("lane") or "").lower()
    brain = (intent.get("stack") or intent.get("brain") or "").lower()

    if action not in ("BUY", "SELL", "SHORT", "COVER"):
        return SeatDecision(
            verdict="pass",
            holder=None,
            intent_brain=brain or None,
            lane=lane or None,
            reason=f"non_routable_action:{action!r}",
        )

    if not lane:
        return SeatDecision(
            verdict="pass",
            holder=None,
            intent_brain=brain or None,
            lane=None,
            reason="intent_missing_lane",
        )

    holder = await get_holder(lane)
    if holder is None:
        return SeatDecision(
            verdict="pass",
            holder=None,
            intent_brain=brain or None,
            lane=lane,
            reason=f"seat_vacant:{lane}_executor",
        )

    if brain != holder:
        return SeatDecision(
            verdict="pass",
            holder=holder,
            intent_brain=brain or None,
            lane=lane,
            reason=f"brain_not_seat_holder:{brain!r}!={holder!r}",
        )

    return SeatDecision(
        verdict="fire",
        holder=holder,
        intent_brain=brain,
        lane=lane,
        reason="seat_holder_fires",
    )


__all__ = [
    "SeatDecision",
    "decide",
    "get_holder",
    "set_holder",
    "list_seats",
]
