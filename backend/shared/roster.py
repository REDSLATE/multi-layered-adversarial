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
  - One brain per role at a time. One role per brain at a time. Swapping
    is atomic; if you put Camaro into "executor" while Camaro currently
    holds "decider", the old seat becomes empty (or you re-fill it via
    a follow-up swap).
  - Opinions get stamped with `posted_as` from the live roster at the
    moment of posting. This is informational; existing scorecard /
    conflict logic continues to key off `runtime` (brain identity).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from auth import get_current_user
from db import db
from namespaces import BRAIN_ROSTER, DISCUSSION_PARTICIPANTS, ROSTER_AUDIT_LOG


ROLES: tuple[str, ...] = ("decider", "executor", "governor", "advisor")
BRAINS: tuple[str, ...] = DISCUSSION_PARTICIPANTS  # ("alpha", "camaro", "chevelle", "redeye")

# Default assignment — matches the doctrine described in the runtime
# patch kits and the user's mental model. Swap freely from here.
DEFAULT_ASSIGNMENTS: dict[str, str] = {
    "decider":  "camaro",
    "executor": "alpha",
    "governor": "chevelle",
    "advisor":  "redeye",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ──────────────────────── singleton accessors ────────────────────────

async def get_roster() -> dict:
    """Return the live roster doc, creating defaults on first read."""
    doc = await db[BRAIN_ROSTER].find_one({"_id": "current"}, {"_id": 0})
    if doc:
        return doc
    seed = {
        "_id": "current",
        "assignments": dict(DEFAULT_ASSIGNMENTS),
        "updated_at": _now_iso(),
        "updated_by": "system_default",
    }
    await db[BRAIN_ROSTER].replace_one({"_id": "current"}, seed, upsert=True)
    return {k: v for k, v in seed.items() if k != "_id"}


async def get_role_of(brain: str) -> Optional[str]:
    """Reverse lookup: which role does this brain currently hold?"""
    r = await get_roster()
    for role, occupant in r["assignments"].items():
        if occupant == brain:
            return role
    return None


async def _audit(action: str, actor: str, payload: dict) -> None:
    await db[ROSTER_AUDIT_LOG].insert_one({
        "ts": _now_iso(),
        "action": action,
        "actor": actor,
        "payload": payload,
    })


# ──────────────────────── models ────────────────────────

RoleT = Literal["decider", "executor", "governor", "advisor"]
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


# ──────────────────────── router ────────────────────────

router = APIRouter(prefix="/admin/roster", tags=["roster"])


@router.get("")
async def get_current(_user: dict = Depends(get_current_user)):
    """Live roster + doctrine reminders for the UI."""
    r = await get_roster()
    return {
        "assignments": r["assignments"],
        "roles": list(ROLES),
        "brains": list(BRAINS),
        "updated_at": r.get("updated_at"),
        "updated_by": r.get("updated_by"),
        "doctrine": (
            "Roster is descriptive metadata. Assigning a brain to "
            "'executor' does not grant execution authority. "
            "may_execute remains schema-pinned False on every endpoint."
        ),
    }


@router.post("/assign")
async def assign(body: AssignIn, user: dict = Depends(get_current_user)):
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
    })
    return await get_current(user)


@router.post("/swap")
async def swap(body: SwapIn, user: dict = Depends(get_current_user)):
    r = await get_roster()
    prev = dict(r["assignments"])
    new_assignments = dict(prev)
    new_assignments[body.role_a], new_assignments[body.role_b] = (
        prev.get(body.role_b),
        prev.get(body.role_a),
    )
    if new_assignments == prev:
        return await get_current(user)

    actor = user.get("email") or "operator"
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
    await db[BRAIN_ROSTER].update_one(
        {"_id": "current"},
        {"$set": {
            "assignments": dict(DEFAULT_ASSIGNMENTS),
            "updated_at": _now_iso(),
            "updated_by": actor,
        }},
        upsert=True,
    )
    await _audit("reset", actor, {"before": prev, "after": DEFAULT_ASSIGNMENTS})
    return await get_current(user)


@router.get("/audit")
async def audit_log(
    limit: int = 100,
    _user: dict = Depends(get_current_user),
):
    rows = await db[ROSTER_AUDIT_LOG].find({}, {"_id": 0}).sort("ts", -1).to_list(min(limit, 500))
    return {"items": rows, "count": len(rows)}
