"""Brain Roster — dynamic role assignment across the four brains.

The roster maps four roles to four brains:
  - decider:  forms the trust/reduce/veto/observation call
  - executor: would carry the order to a broker IF execution were enabled
  - governor: audits, gates, freezes — never decides, never executes
  - advisor:  whispers context to the decider; never decides, never executes

Defaults match original doctrine (camaro / alpha / chevelle / redeye in that
order). Operator can swap on demand. Roster changes are audit-logged.

Doctrine guards:
  - The roster is descriptive metadata. It does NOT touch `may_execute`,
    which remains schema-pinned False everywhere.
  - Tenure (how long a brain has held a role) is observability only.
    It cannot affect execution, scoring authority, or any gate.
  - One brain per role at a time. One role per brain at a time. Swapping
    is atomic; if you put Camaro into "executor" while Camaro currently
    holds "decider", the old seat becomes empty (or you re-fill it via
    a follow-up swap).
  - Opinions get stamped with `posted_as` from the live roster at the
    moment of posting. This is informational; existing scorecard /
    conflict logic continues to key off `runtime` (brain identity).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from auth import get_current_user
from db import db
from namespaces import BRAIN_ELIGIBILITY, BRAIN_ROSTER, DISCUSSION_PARTICIPANTS, ROSTER_AUDIT_LOG


ROLES: tuple[str, ...] = (
    "decider", "executor", "governor", "advisor", "opponent", "crypto",
    # ─── Crypto lane (isolated execution authority, 2026-02-15) ───────
    # The crypto lane runs its own council — governor, advisor, opponent —
    # so equity policy never leaks into crypto routing. `crypto` (above)
    # is the crypto EXECUTOR seat (legacy name retained for back-compat
    # with the existing eligibility matrix). These three add the rest of
    # the crypto council. The doctrine: equity reads default seats,
    # crypto reads crypto_* seats; if a crypto_* seat is vacant it
    # falls back to the default. See `_seat_holder(role, lane=...)`.
    "crypto_advisor", "crypto_governor", "crypto_opponent",
)
BRAINS: tuple[str, ...] = DISCUSSION_PARTICIPANTS  # ("alpha", "camaro", "chevelle", "redeye")

# Default assignment — matches the doctrine. REDEYE defaults to opponent
# (its training intent is adversarial / argue-the-contrary). `advisor`
# starts vacated — there's no doctrinal pick for a neutral-counsel seat,
# and we'd rather leave it empty than miscast a brain. `crypto` also
# starts vacated — operator chooses which brain takes the crypto-only
# execution chair (it's a dedicated specialist seat, not a default).
DEFAULT_ASSIGNMENTS: dict[str, Optional[str]] = {
    "decider":  "camaro",
    "executor": "alpha",
    "governor": "chevelle",
    "advisor":  None,        # operator-assigned
    "opponent": "redeye",
    "crypto":   None,        # operator-assigned: dedicated crypto executor
    # Crypto council seats — all vacant until operator slots them.
    # When vacant, the council falls back to the equity seat for that role.
    "crypto_advisor":  None,
    "crypto_governor": None,
    "crypto_opponent": None,
}

# Default eligibility — every brain is eligible for every seat by
# default. Doctrine: identity is NOT restriction. The seat itself
# carries the function and the restrictions; pulling a brain into a
# seat applies that seat's policy. The operator chooses who fits where.
# Eligibility can be tightened per-brain per-seat later via the operator
# console if a brain's training intent makes a specific seat a bad fit.
_ALL_TRUE = {role: True for role in ROLES}
DEFAULT_ELIGIBILITY: dict[str, dict[str, bool]] = {
    "alpha":    dict(_ALL_TRUE),
    "camaro":   dict(_ALL_TRUE),
    "chevelle": dict(_ALL_TRUE),
    "redeye":   dict(_ALL_TRUE),
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ──────────────────────── singleton accessors ────────────────────────

async def get_roster() -> dict:
    """Return the live roster doc, creating defaults on first read."""
    doc = await db[BRAIN_ROSTER].find_one({"_id": "current"}, {"_id": 0})
    if doc:
        # Backfill seat_epoch on legacy docs (one if missing).
        if "seat_epoch" not in doc:
            doc["seat_epoch"] = 1
        return doc
    seed = {
        "_id": "current",
        "assignments": dict(DEFAULT_ASSIGNMENTS),
        # seat_epoch increments on every reassignment. Every opinion /
        # stance / decision stamped with this number can be later joined
        # back to the roster history that was in effect at write time.
        "seat_epoch": 1,
        "updated_at": _now_iso(),
        "updated_by": "system_default",
    }
    await db[BRAIN_ROSTER].replace_one({"_id": "current"}, seed, upsert=True)
    return {k: v for k, v in seed.items() if k != "_id"}


async def _bump_epoch() -> int:
    """Increment + return the new seat_epoch. Called by any roster mutation."""
    r = await db[BRAIN_ROSTER].find_one_and_update(
        {"_id": "current"},
        {"$inc": {"seat_epoch": 1}, "$set": {"updated_at": _now_iso()}},
        upsert=True,
        return_document=True,
        projection={"_id": 0, "seat_epoch": 1},
    )
    return int(r.get("seat_epoch") or 1)


async def get_role_of(brain: str) -> Optional[str]:
    """Reverse lookup: which role does this brain currently hold?"""
    r = await get_roster()
    for role, occupant in r["assignments"].items():
        if occupant == brain:
            return role
    return None


async def get_eligibility() -> dict[str, dict[str, bool]]:
    """Return the live eligibility matrix, seeding defaults on first read."""
    doc = await db[BRAIN_ELIGIBILITY].find_one({"_id": "current"}, {"_id": 0})
    if doc and isinstance(doc.get("matrix"), dict):
        # Ensure every (brain, role) cell has a value (default False for
        # missing cells — fail closed if the operator adds a new role).
        matrix = {b: {r: False for r in ROLES} for b in BRAINS}
        for brain, roles in doc["matrix"].items():
            if brain not in matrix:
                continue
            for role, allowed in (roles or {}).items():
                if role in matrix[brain]:
                    matrix[brain][role] = bool(allowed)
        return matrix
    # Seed defaults
    seed = {
        "_id": "current",
        "matrix": {b: dict(DEFAULT_ELIGIBILITY[b]) for b in BRAINS},
        "updated_at": _now_iso(),
        "updated_by": "system_default",
    }
    await db[BRAIN_ELIGIBILITY].replace_one({"_id": "current"}, seed, upsert=True)
    return {b: dict(DEFAULT_ELIGIBILITY[b]) for b in BRAINS}


async def _ensure_assignment_eligible(role: str, brain: Optional[str]) -> None:
    """Raise 400 if this (role, brain) pair is currently disallowed by
    the eligibility matrix. Vacating (brain=None) is always allowed."""
    if brain is None:
        return
    matrix = await get_eligibility()
    if not matrix.get(brain, {}).get(role, False):
        raise HTTPException(
            status_code=400,
            detail=(
                f"{brain} is not eligible for the {role} seat. "
                f"Toggle eligibility on /api/admin/roster/eligibility "
                f"if you intend to allow it."
            ),
        )


async def _audit(action: str, actor: str, payload: dict) -> None:
    await db[ROSTER_AUDIT_LOG].insert_one({
        "ts": _now_iso(),
        "action": action,
        "actor": actor,
        "payload": payload,
    })


# ──────────────────────── models ────────────────────────

RoleT = Literal[
    "decider", "executor", "governor", "advisor", "opponent", "crypto",
    "crypto_advisor", "crypto_governor", "crypto_opponent",
]
BrainT = Literal["alpha", "camaro", "chevelle", "redeye"]


class AssignIn(BaseModel):
    """Assign a single brain to a single role.

    If `brain` is already in another role, that other role becomes empty
    (we don't auto-swap). Use `/swap` to atomically exchange two roles.
    """
    role: RoleT
    brain: Optional[BrainT] = Field(
        default=None,
        description="None = vacate the role; otherwise the brain to place there",
    )


class SwapIn(BaseModel):
    """Atomically swap occupants of two roles. The two roles must differ."""
    role_a: RoleT
    role_b: RoleT

    @field_validator("role_b")
    @classmethod
    def _different(cls, v: str, info) -> str:
        a = info.data.get("role_a")
        if a == v:
            raise ValueError("role_a and role_b must differ")
        return v


class EligibilitySetIn(BaseModel):
    """Toggle a single (brain, role) cell."""
    brain: BrainT
    role: RoleT
    allowed: bool


# ──────────────────────── router ────────────────────────

router = APIRouter(prefix="/admin/roster", tags=["roster"])


@router.get("")
async def get_current(_user: dict = Depends(get_current_user)):
    """Live roster + eligibility matrix + doctrine reminders for the UI."""
    r = await get_roster()
    elig = await get_eligibility()
    from shared.seat_policy import SEAT_POLICY
    return {
        "assignments": r["assignments"],
        "roles": list(ROLES),
        "brains": list(BRAINS),
        "eligibility": elig,
        "seat_epoch": r.get("seat_epoch", 1),
        "policy": SEAT_POLICY,
        "updated_at": r.get("updated_at"),
        "updated_by": r.get("updated_by"),
        "doctrine": (
            "Identity does not grant authority — seat policy does. "
            "Assigning a brain to 'executor' attaches the executor "
            "permissions snapshot to every stance that brain posts "
            "while in the seat. may_execute remains schema-pinned "
            "False at every endpoint in Phase 1."
        ),
    }


@router.post("/assign")
async def assign(body: AssignIn, user: dict = Depends(get_current_user)):
    # Eligibility gate — refuse to place a brain in a seat the operator
    # has marked disallowed.
    await _ensure_assignment_eligible(body.role, body.brain)

    r = await get_roster()
    prev = dict(r["assignments"])
    new_assignments = dict(prev)

    # If the brain already holds a different role, vacate that role.
    if body.brain:
        for role, occupant in list(new_assignments.items()):
            if occupant == body.brain and role != body.role:
                new_assignments[role] = None

    new_assignments[body.role] = body.brain

    if new_assignments == prev:
        # No-op; don't audit-log a non-change.
        return await get_current(user)

    actor = user.get("email") or "operator"
    new_epoch = await _bump_epoch()
    await db[BRAIN_ROSTER].update_one(
        {"_id": "current"},
        {"$set": {
            "assignments": new_assignments,
            "updated_at": _now_iso(),
            "updated_by": actor,
        }},
        upsert=True,
    )
    await _audit("assign", actor, {
        "role": body.role,
        "from": prev.get(body.role),
        "to": body.brain,
        "before": prev,
        "after": new_assignments,
        "seat_epoch": new_epoch,
    })
    return await get_current(user)


@router.post("/swap")
async def swap(body: SwapIn, user: dict = Depends(get_current_user)):
    r = await get_roster()
    prev = dict(r["assignments"])
    # Eligibility gate — the brain moving INTO role_a must be eligible
    # for role_a, and vice versa for role_b. Vacant moves are fine.
    await _ensure_assignment_eligible(body.role_a, prev.get(body.role_b))
    await _ensure_assignment_eligible(body.role_b, prev.get(body.role_a))
    new_assignments = dict(prev)
    new_assignments[body.role_a], new_assignments[body.role_b] = (
        prev.get(body.role_b),
        prev.get(body.role_a),
    )
    if new_assignments == prev:
        return await get_current(user)

    actor = user.get("email") or "operator"
    new_epoch = await _bump_epoch()
    await db[BRAIN_ROSTER].update_one(
        {"_id": "current"},
        {"$set": {
            "assignments": new_assignments,
            "updated_at": _now_iso(),
            "updated_by": actor,
        }},
        upsert=True,
    )
    await _audit("swap", actor, {
        "role_a": body.role_a,
        "role_b": body.role_b,
        "before": prev,
        "after": new_assignments,
        "seat_epoch": new_epoch,
    })
    return await get_current(user)


@router.post("/reset")
async def reset_defaults(user: dict = Depends(get_current_user)):
    """Restore the doctrine default roster (camaro/alpha/chevelle/redeye)."""
    r = await get_roster()
    prev = dict(r["assignments"])
    if prev == DEFAULT_ASSIGNMENTS:
        return await get_current(user)
    actor = user.get("email") or "operator"
    new_epoch = await _bump_epoch()
    await db[BRAIN_ROSTER].update_one(
        {"_id": "current"},
        {"$set": {
            "assignments": dict(DEFAULT_ASSIGNMENTS),
            "updated_at": _now_iso(),
            "updated_by": actor,
        }},
        upsert=True,
    )
    await _audit("reset", actor, {
        "before": prev, "after": DEFAULT_ASSIGNMENTS, "seat_epoch": new_epoch,
    })
    return await get_current(user)


@router.get("/audit")
async def audit_log(
    limit: int = 100,
    _user: dict = Depends(get_current_user),
):
    rows = await db[ROSTER_AUDIT_LOG].find({}, {"_id": 0}).sort("ts", -1).to_list(min(limit, 500))
    return {"items": rows, "count": len(rows)}


# ──────────────────────── eligibility matrix ────────────────────────

@router.get("/eligibility")
async def get_eligibility_matrix(_user: dict = Depends(get_current_user)):
    return {
        "matrix": await get_eligibility(),
        "roles": list(ROLES),
        "brains": list(BRAINS),
        "doctrine": (
            "Operator-controlled access list deciding which seats each "
            "brain may hold. Like the roster itself, this is descriptive — "
            "it does not grant execution. It constrains future role "
            "assignments only."
        ),
    }


@router.post("/eligibility")
async def set_eligibility_cell(
    body: EligibilitySetIn, user: dict = Depends(get_current_user),
):
    matrix = await get_eligibility()
    current_value = matrix.get(body.brain, {}).get(body.role, False)
    if current_value == body.allowed:
        return await get_eligibility_matrix(user)

    # Safety: if disallowing a brain from a role they CURRENTLY occupy,
    # refuse — operator must vacate or swap first. This avoids the
    # confusing state of "brain X is in role Y but matrix says no".
    if not body.allowed:
        roster = await get_roster()
        if roster["assignments"].get(body.role) == body.brain:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"cannot disallow {body.brain} from {body.role} while "
                    f"they currently occupy that seat. Vacate or swap first."
                ),
            )

    matrix[body.brain][body.role] = body.allowed
    actor = user.get("email") or "operator"
    await db[BRAIN_ELIGIBILITY].update_one(
        {"_id": "current"},
        {"$set": {
            "matrix": matrix,
            "updated_at": _now_iso(),
            "updated_by": actor,
        }},
        upsert=True,
    )
    await _audit("eligibility_set", actor, {
        "brain": body.brain,
        "role": body.role,
        "allowed": body.allowed,
    })
    return await get_eligibility_matrix(user)


@router.post("/eligibility/reset")
async def reset_eligibility(user: dict = Depends(get_current_user)):
    """Restore the doctrine default eligibility matrix."""
    matrix = await get_eligibility()
    default = {b: dict(DEFAULT_ELIGIBILITY[b]) for b in BRAINS}
    if matrix == default:
        return await get_eligibility_matrix(user)
    # Make sure resetting doesn't strand any current occupant out of
    # eligibility. If it would, refuse and tell the operator which
    # roster move to make first.
    roster = await get_roster()
    conflicts = []
    for role, occupant in roster["assignments"].items():
        if occupant and not default.get(occupant, {}).get(role, False):
            conflicts.append({"role": role, "occupant": occupant})
    if conflicts:
        raise HTTPException(
            status_code=400,
            detail=(
                f"reset would strand current occupants: {conflicts}. "
                f"Vacate or swap those roles first, then reset."
            ),
        )
    actor = user.get("email") or "operator"
    await db[BRAIN_ELIGIBILITY].update_one(
        {"_id": "current"},
        {"$set": {
            "matrix": default,
            "updated_at": _now_iso(),
            "updated_by": actor,
        }},
        upsert=True,
    )
    await _audit("eligibility_reset", actor, {"matrix": default})
    return await get_eligibility_matrix(user)


# ──────────────────────── tenure KPI ────────────────────────

def _churn_state(swaps_90d: int) -> str:
    """Map 90-day swap count to a stability heuristic.

    LOW (≤4): operator has chosen the seating and is letting it run.
    MEDIUM (5–12): active tuning — still iterating on who fits where.
    HIGH (>12): churning — either the roles are misdefined or the
                brains are mid-training and the operator is hunting.
    """
    if swaps_90d <= 4:
        return "LOW"
    if swaps_90d <= 12:
        return "MEDIUM"
    return "HIGH"


def _format_tenure_days(days: float | None) -> str:
    """Pretty-print a tenure for UI consumption. Sub-day → hours."""
    if days is None:
        return "—"
    if days < (1.0 / 24):
        return "<1h"
    if days < 1:
        return f"{int(days * 24)}h"
    return f"{int(days)}d"


@router.get("/tenure")
async def tenure(_user: dict = Depends(get_current_user)):
    """Role Tenure KPI — per-role + aggregate.

    Doctrine: observability only. Tenure cannot affect execution,
    scoring authority, or any gate. It exists so the operator can see
    how stable each seat has been, and whether the roster is settling
    or still being tuned.
    """
    roster = await get_roster()
    current: dict[str, Optional[str]] = roster["assignments"]
    roster_updated_at: Optional[str] = roster.get("updated_at")

    # Walk the audit log oldest → newest; for every role, capture the
    # most recent moment its current occupant entered (i.e. the latest
    # `payload.after[role] == current_brain` event preceded by
    # `payload.before[role] != current_brain`).
    log = await db[ROSTER_AUDIT_LOG].find(
        {}, {"_id": 0}
    ).sort("ts", 1).to_list(2000)

    enter_ts: dict[str, Optional[str]] = {r: None for r in ROLES}
    # previous_role[brain] = the role this brain was in just before its
    # current one (None if no history exists).
    previous_role_for_brain: dict[str, Optional[str]] = {}

    for role in ROLES:
        brain = current.get(role)
        if not brain:
            enter_ts[role] = None
            continue
        # Default: if no log entry ever placed this brain into this
        # role, the seating is the original seed. Use the roster doc's
        # updated_at as the entry timestamp.
        entered_at = roster_updated_at
        last_role_for_brain_before_current: Optional[str] = None
        for entry in log:
            payload = entry.get("payload", {}) or {}
            before = payload.get("before", {}) or {}
            after = payload.get("after", {}) or {}
            # Brain placed INTO this role (transition)
            if after.get(role) == brain and before.get(role) != brain:
                entered_at = entry["ts"]
                # Where was the brain just before? Look at `before`.
                for r2, b2 in before.items():
                    if b2 == brain and r2 != role:
                        last_role_for_brain_before_current = r2
                        break
        enter_ts[role] = entered_at
        if last_role_for_brain_before_current:
            previous_role_for_brain[brain] = last_role_for_brain_before_current

    # days_in_role
    now = datetime.now(timezone.utc)
    days_in_role: dict[str, Optional[float]] = {}
    for role, ts in enter_ts.items():
        if not ts or not current.get(role):
            days_in_role[role] = None
            continue
        try:
            entered = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            days_in_role[role] = None
            continue
        days_in_role[role] = max((now - entered).total_seconds() / 86_400, 0.0)

    # total_swaps over the last 90 days, plus all-time count
    cutoff_90 = (now - timedelta(days=90)).isoformat()
    total_swaps_90d = await db[ROSTER_AUDIT_LOG].count_documents(
        {"ts": {"$gte": cutoff_90}}
    )
    total_swaps_all = await db[ROSTER_AUDIT_LOG].count_documents({})

    # average tenure across the four roles (skip vacant seats)
    tenures = [v for v in days_in_role.values() if v is not None]
    avg_tenure = round(sum(tenures) / len(tenures), 2) if tenures else 0.0

    # last_swap
    last_swap_doc = log[-1] if log else None
    last_swap = None
    if last_swap_doc:
        last_swap = {
            "ts": last_swap_doc["ts"],
            "action": last_swap_doc["action"],
            "actor": last_swap_doc.get("actor"),
            "payload": last_swap_doc.get("payload", {}),
            "age_days": max(
                (now - datetime.fromisoformat(
                    last_swap_doc["ts"].replace("Z", "+00:00")
                )).total_seconds() / 86_400, 0.0,
            ),
        }

    per_role = []
    for role in ROLES:
        brain = current.get(role)
        d = days_in_role.get(role)
        per_role.append({
            "role": role,
            "brain": brain,
            "current_role_started_at": enter_ts.get(role),
            "days_in_role": d,
            "tenure_display": _format_tenure_days(d) if brain else "—",
            "previous_role": previous_role_for_brain.get(brain) if brain else None,
        })

    return {
        "per_role": per_role,
        "total_swaps_90d": total_swaps_90d,
        "total_swaps_all_time": total_swaps_all,
        "average_tenure_days": avg_tenure,
        "average_tenure_display": _format_tenure_days(avg_tenure),
        "churn_state": _churn_state(total_swaps_90d),
        "last_swap": last_swap,
        "doctrine_invariant": (
            "Tenure must never affect execution. It informs trust, "
            "stability, and review priority only."
        ),
    }
