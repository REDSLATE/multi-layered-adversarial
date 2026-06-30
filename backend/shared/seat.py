"""Seat — the ONE module that decides whose opinion fires.

Doctrine (2026-02-27 architectural reduction, operator-pinned):

    Seat carries the FUNCTION. Brain keeps its PERSONALITY.

    Each lane has FOUR seats, each a distinct function:
        strategist  — proposes the trade (the brain emits)
        governor    — sets the lane's risk regime (size multiplier)
        executor    — authorizes routing to the broker
        auditor     — recorded on the executions row for post-pass review

    Brains rotate INTO seats. Camino is not "the trend brain forever";
    Camino currently holds (e.g.) the equity executor seat. Tomorrow
    a different brain may hold it. Personality is the strategy each
    brain runs (in `shared/brains/<name>/strategy.py`). Authority
    is the seat.

    ONE PASS. `Seat.decide(intent)` returns ONE `SeatDecision` that
    contains:
        verdict ('fire' | 'pass')
        all 4 seat holders for the lane
        the governor's risk multiplier (already applied — no callback)
        the auditor's identity (recorded on the executions row)
        a single reason string

    No callbacks. No "auditor objects to executor" loops. No
    council vote. No consensus pool. No re-evaluation. The intent
    either fires this pass or it doesn't — and the operator can rotate
    seats at any time to change tomorrow's behavior.

Storage:
    seat_registry — one doc per (lane, role). _id = "<lane>:<role>".
        {
          holder: str | None,
          risk_multiplier: float,    # governor only; 1.0 default; ignored for other roles
          since: iso,
          assigned_by: email,
          reason: str,
          last_changed_at: iso,
        }

The Seat is INTENTIONALLY thin. It does not:
    * modify the brain's BUY/SELL/HOLD (no wrappers)
    * run a council vote
    * score dissent / evidence quality
    * apply a tier policy
    * re-evaluate after the fact

What it DOES:
    1. Look up all 4 seat holders for the intent's lane.
    2. Verify the emitting brain holds either the strategist or executor
       seat (strategist proposes; executor self-fires).
    3. Read the governor's `risk_multiplier`.
    4. Stamp the auditor on the decision for the executions row.
    5. Return a single `SeatDecision` — fire or pass.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from db import db


_SEAT_COLL = "seat_registry"

Verdict = Literal["fire", "pass"]

# Role identifiers. Order matters for display only.
ROLES = ("strategist", "governor", "executor", "auditor")
LANES = ("equity", "crypto")

# Angel-named seat labels (operator doctrine — pinned 2026-02-27).
# The seat's NAME is the angel. The seat's FUNCTION is the role.
# The brain currently sitting in the seat is the holder. Angels are
# constants of the architecture; brains rotate; functions never change.
SEAT_ANGELS: dict[tuple[str, str], str] = {
    ("equity", "strategist"): "Raziel",
    ("equity", "governor"):   "Nuriel",
    ("equity", "executor"):   "Paschar",
    ("equity", "auditor"):    "Sariel",
    ("crypto", "strategist"): "Remiel",
    ("crypto", "governor"):   "Cassiel",
    ("crypto", "executor"):   "Israfel",
    ("crypto", "auditor"):    "Zadkiel",
}


def angel_for(lane: str, role: str) -> Optional[str]:
    """Return the angel name for a (lane, role), or None if unknown."""
    return SEAT_ANGELS.get(((lane or "").lower(), (role or "").lower()))


@dataclass(frozen=True)
class SeatDecision:
    verdict: Verdict
    lane: Optional[str]
    intent_brain: Optional[str]

    # The four role-holders for the lane, in one snapshot.
    strategist: Optional[str]
    governor: Optional[str]
    executor: Optional[str]
    auditor: Optional[str]

    # Angel-named seat labels, in one snapshot. The angel is the
    # seat's NAME; the brain (above) is who holds it; the role
    # (key in the dict) is the function. All three live together
    # on the decision so the executions row records the full
    # context with no joins.
    angels: dict   # {"strategist": "Raziel", ...}

    # Governor's one-pass effect on sizing. Default 1.0 (no change).
    # Applied by the caller to the intent's notional. NOT re-read
    # anywhere else.
    risk_multiplier: float

    reason: str


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _seat_id(lane: str, role: str) -> str:
    return f"{(lane or '').lower()}:{(role or '').lower()}"


async def get_holder(lane: str, role: str = "executor") -> Optional[str]:
    """Current holder of (lane, role), or None if vacant.

    Reads from `seat_registry` first (new home). Falls back to the
    legacy `shared_brain_roster.assignments` to preserve existing
    operator seat assignments during the architectural reduction.

    Legacy roster key convention:
        equity executor    → "executor"
        equity strategist  → "strategist"
        equity governor    → "governor"
        equity auditor     → "auditor"
        crypto executor    → "crypto_executor"
        crypto strategist  → "crypto_strategist"
        crypto governor    → "crypto_governor"
        crypto auditor     → "crypto_auditor"
    """
    if not lane:
        return None
    doc = await db[_SEAT_COLL].find_one(
        {"_id": _seat_id(lane, role)}, {"_id": 0, "holder": 1}
    )
    if doc and doc.get("holder"):
        return doc["holder"]

    lane_l = (lane or "").lower()
    role_l = (role or "executor").lower()
    legacy_keys = [role_l] if lane_l == "equity" else []
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


async def get_lane_seats(lane: str) -> dict[str, Optional[str]]:
    """One snapshot of all 4 role holders for a lane."""
    out: dict[str, Optional[str]] = {}
    for r in ROLES:
        out[r] = await get_holder(lane, r)
    return out


async def get_governor_multiplier(lane: str) -> float:
    """The governor's current risk multiplier for the lane. Default 1.0.
    Bounded [0.0, 2.0] so a stale or corrupted value can't blow caps."""
    doc = await db[_SEAT_COLL].find_one(
        {"_id": _seat_id(lane, "governor")},
        {"_id": 0, "risk_multiplier": 1},
    )
    raw = (doc or {}).get("risk_multiplier")
    if raw is None:
        return 1.0
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return 1.0
    return max(0.0, min(2.0, v))


async def set_holder(
    lane: str,
    holder: Optional[str],
    *,
    role: str = "executor",
    assigned_by: str = "operator",
    reason: str = "",
    risk_multiplier: Optional[float] = None,
) -> dict:
    """Assign or clear a seat. Upserts the registry row.

    The angel name for (lane, role) is also stamped on the row so
    every read is self-describing (no join against SEAT_ANGELS).
    `risk_multiplier` is governor-only; ignored for other roles.
    Bounded [0.0, 2.0] when present.
    """
    now = _now_iso()
    sid = _seat_id(lane, role)
    set_fields: dict[str, Any] = {
        "holder": holder,
        "angel": angel_for(lane, role),
        "since": now if holder else None,
        "assigned_by": assigned_by if holder else None,
        "reason": reason,
        "last_changed_at": now,
        "lane": (lane or "").lower(),
        "role": (role or "").lower(),
    }
    if role.lower() == "governor":
        if risk_multiplier is None:
            set_fields["risk_multiplier"] = 1.0
        else:
            set_fields["risk_multiplier"] = max(
                0.0, min(2.0, float(risk_multiplier))
            )
    await db[_SEAT_COLL].update_one(
        {"_id": sid},
        {"$set": set_fields, "$setOnInsert": {"_id": sid}},
        upsert=True,
    )
    return {"ok": True, **set_fields, "_id": sid}


async def list_seats() -> dict[str, dict]:
    """Every seat row, keyed by `<lane>:<role>`. Returns the registry
    row PLUS the angel name (filled in from SEAT_ANGELS even when the
    seat was created before the angel field existed). Used by the
    Seat tile."""
    out: dict[str, dict] = {}
    seen: set[str] = set()
    async for row in db[_SEAT_COLL].find({}):
        sid = row.pop("_id", None)
        if not sid:
            continue
        if not row.get("angel"):
            row["angel"] = angel_for(row.get("lane"), row.get("role"))
        out[sid] = row
        seen.add(sid)
    # Surface every angel-named seat even if the row hasn't been
    # written yet — operator sees the full board, including vacancies.
    for (lane, role), angel in SEAT_ANGELS.items():
        sid = _seat_id(lane, role)
        if sid not in seen:
            out[sid] = {
                "lane": lane,
                "role": role,
                "angel": angel,
                "holder": None,
                "vacant": True,
            }
    return out


async def decide(intent: dict[str, Any]) -> SeatDecision:
    """ONE PASS. Returns a single SeatDecision — fire or pass.

    Rules (linear, no loops):
        1. action must be directional (BUY/SELL/SHORT/COVER).
        2. lane must be present.
        3. executor seat must be filled.
        4. emitting brain must hold strategist OR executor seat.
        5. governor's risk_multiplier is read once and stamped on the
           returned decision. Caller multiplies notional by it.
        6. auditor is stamped on the decision (for the executions row).

    Anything that fails this single pass returns `pass`. Caller
    writes one executions row and stops. No re-route. No retry on
    the same intent (auto_router stamps gate_state so the next tick
    skips it).
    """
    action = (intent.get("action") or "").upper()
    lane = (intent.get("lane") or "").lower()
    brain = (intent.get("stack") or intent.get("brain") or "").lower()

    seats = await get_lane_seats(lane) if lane else {r: None for r in ROLES}
    mult = await get_governor_multiplier(lane) if lane else 1.0

    angels = {r: angel_for(lane, r) for r in ROLES} if lane else {r: None for r in ROLES}

    base = dict(
        lane=lane or None,
        intent_brain=brain or None,
        strategist=seats.get("strategist"),
        governor=seats.get("governor"),
        executor=seats.get("executor"),
        auditor=seats.get("auditor"),
        angels=angels,
        risk_multiplier=mult,
    )

    if action not in ("BUY", "SELL", "SHORT", "COVER"):
        return SeatDecision(
            verdict="pass",
            reason=f"non_routable_action:{action!r}",
            **base,
        )
    if not lane:
        return SeatDecision(
            verdict="pass", reason="intent_missing_lane", **base,
        )
    if not seats.get("executor"):
        return SeatDecision(
            verdict="pass",
            reason=f"executor_seat_vacant:{lane}",
            **base,
        )
    if brain not in {seats.get("strategist"), seats.get("executor")}:
        return SeatDecision(
            verdict="pass",
            reason=(
                f"unauthorized_brain:{brain!r} holds neither "
                f"strategist({seats.get('strategist')!r}) nor "
                f"executor({seats.get('executor')!r}) seat for {lane}"
            ),
            **base,
        )

    return SeatDecision(
        verdict="fire",
        reason=(
            "strategist_proposes" if brain == seats.get("strategist")
            else "executor_self_fires"
        ),
        **base,
    )


__all__ = [
    "ROLES",
    "LANES",
    "SEAT_ANGELS",
    "SeatDecision",
    "angel_for",
    "decide",
    "get_holder",
    "get_lane_seats",
    "get_governor_multiplier",
    "set_holder",
    "list_seats",
]
