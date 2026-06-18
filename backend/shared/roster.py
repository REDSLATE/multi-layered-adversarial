"""Brain Roster — dynamic role assignment across the four brains.

Doctrine (2026-05-26 — operator clarification):
  Final equity seat set is FIVE: strategist, executor, auditor, governor,
  opponent. The historical `decider` seat has been renamed to
  `strategist` (same function — forms the trust/reduce/veto/observation
  call). Legacy `decider` reads are alias-rewritten to `strategist`.

  Seat eligibility — OPERATOR DOCTRINE (2026-05-26 revision):
    * ALL seats are open to ALL brains by default — strategist,
      executor, auditor, opponent, crypto, and every `crypto_*` slot.
      Identity does not grant authority; seat policy does. The seat's
      policy is what determines abilities.
    * EXCEPTION: the `governor` seat (and its crypto twin
      `crypto_governor`) are EXCLUSIVE TO CHEVELLE AND REDEYE. No
      other brain may hold either governor slot. This is a hard
      doctrine line, enforced both at default seeding (defaults to
      False for alpha/camaro) and at runtime via
      `_GOVERNOR_EXCLUSIVE_BRAINS` — the eligibility-toggle endpoint
      refuses any request that would let alpha or camaro hold a
      governor seat.
    * The operator may tighten any non-governor cell through the
      eligibility UI if a brain's training intent makes a specific
      seat a bad fit. They cannot LOOSEN governor.

Roles (legacy semantic anchors retained):
  - strategist: forms the trust/reduce/veto/observation call
  - executor:   would carry the order to a broker IF execution were enabled
  - auditor:    post-trade reviewer; analyzes outcomes vs intent
  - governor:   audits, gates, freezes — never decides, never executes
  - opponent:   argues the contrary case; never decides, never executes

Defaults: camaro = strategist, alpha = executor, redeye = opponent,
chevelle = governor. Auditor starts vacant (operator-assigned).
Operator can swap on demand within eligibility. Changes are audit-logged.

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
from typing import Iterable, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from auth import get_current_user
from db import db
from namespaces import BRAIN_ELIGIBILITY, BRAIN_ROSTER, DISCUSSION_PARTICIPANTS, ROSTER_AUDIT_LOG, SHARED_EXECUTOR_SEAT


ROLES: tuple[str, ...] = (
    # ─── The canonical 8-seat roster (operator pin, 2026-05-31) ───────
    # Equity lane:
    "strategist", "executor", "governor", "auditor",
    # Crypto lane (lane-isolated execution authority):
    "crypto_strategist", "crypto", "crypto_governor", "crypto_auditor",
)
BRAINS: tuple[str, ...] = DISCUSSION_PARTICIPANTS  # ("camino", "barracuda", "hellcat", "gto")

# Default seat → brain assignment. Operator-pinned defaults for the
# equity lane (strategist=camaro, executor=alpha, governor=chevelle);
# auditor + every crypto seat start vacant so the operator slots them
# explicitly. A brain MAY hold one equity seat AND one crypto seat
# simultaneously — eligibility (below) only restricts Governor seats.
# Default seat → brain assignment. Operator-pinned defaults for the
# equity lane (strategist=camaro, executor=alpha, governor=chevelle).
# All other seats start vacant. CRYPTO LANE INTENTIONALLY VACANT:
# per Paradox v2 doctrine, restrictions belong to the SEAT (capital,
# trust list, autonomy state) not to the BRAIN. The seat decides who
# it trusts; defaulting a brain into a seat re-introduces the exact
# coupling we're removing. Operator (or the seat's verifier-driven
# promotion path) is the only way a brain enters the crypto seat.
DEFAULT_ASSIGNMENTS: dict[str, Optional[str]] = {
    "strategist":        "barracuda",
    "executor":          "camino",
    "governor":          "hellcat",
    "auditor":           None,
    "crypto_strategist": None,
    "crypto":            None,
    "crypto_governor":   None,
    "crypto_auditor":    None,
}

# ─── Default eligibility (2026-05-26 — operator doctrine, governor-exclusive) ──
# Doctrine: IDENTITY DOES NOT GRANT AUTHORITY. SEAT POLICY DOES.
# All brains are eligible for all seats by default EXCEPT the
# `governor` and `crypto_governor` seats — those two are exclusive to
# Chevelle and RedEye and enforced at the eligibility-toggle endpoint
# (see `_GOVERNOR_EXCLUSIVE_BRAINS` and the validator that refuses any
# request to set alpha/camaro to governor=True). The operator may
# tighten any non-governor cell; they cannot loosen governor.
_ALL_TRUE = {role: True for role in ROLES}

# Seats Chevelle/RedEye exclusively may hold. Equity + crypto twins.
_GOVERNOR_EXCLUSIVE_SEATS: tuple[str, ...] = ("governor", "crypto_governor")
_GOVERNOR_EXCLUSIVE_BRAINS: tuple[str, ...] = ("hellcat", "gto")


def _build_default_eligibility() -> dict[str, dict[str, bool]]:
    """Per-brain seat-eligibility map. All True except governor cells
    for non-Chevelle/RedEye brains."""
    out: dict[str, dict[str, bool]] = {}
    for brain in BRAINS:
        row = dict(_ALL_TRUE)
        if brain not in _GOVERNOR_EXCLUSIVE_BRAINS:
            for seat in _GOVERNOR_EXCLUSIVE_SEATS:
                row[seat] = False
        out[brain] = row
    return out


DEFAULT_ELIGIBILITY: dict[str, dict[str, bool]] = _build_default_eligibility()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ──────────────────────── singleton accessors ────────────────────────

# DOCTRINE PIN (2026-02-17): DO NOT REMOVE THESE ALIAS REWRITES.
#
# 25% of `sovereign_audit_log` rows in production (1,363 / 5,463 as of
# 2026-02-17 audit) still carry the legacy `decider` / `crypto_decider`
# keys from before the 2026-05-24 rename. The alias-rewrite layer is
# LOAD-BEARING for read-path translation of historical audit data:
# stripping it would corrupt audit-log read responses across roughly
# a quarter of MC's history.
#
# Any future "cleanup" pass that wants to remove these MUST first run
# a one-shot migration script that backfills the canonical keys
# across every collection that ever stored a role/seat/posted_as
# field — and only then remove the rewrite. The migration is its own
# multi-step undertaking, NOT a routine code cleanup. Until that
# migration ships, these aliases are mandatory.
#
# Same logic applies to the `opponent` → `auditor` rewrite below; that
# rename is more recent (2026-05-27) and historical rows are still
# being produced in some legacy code paths.
_LEGACY_ROLE_REWRITES: dict[str, str] = {
    "decider": "strategist",
    "crypto_decider": "crypto_strategist",
    # 2026-05-27: Opponent seat merged into Auditor. Any roster doc,
    # API request, or sidecar that still references `opponent` is
    # silently rewritten to `auditor`. The pre-trade adversarial
    # argument and the post-trade review are now the same seat's
    # responsibility — same brain, two time windows.
    "opponent": "auditor",
    "crypto_opponent": "crypto_auditor",
    # 2026-06-18: `crypto_executor` was a legacy alias for the canonical
    # `crypto` seat. `SEAT_ALIASES` in `seat_policy.py` already had
    # this mapping, but `_LEGACY_ROLE_REWRITES` (the migration table
    # `get_roster()` walks on every read to rewrite stored doc keys)
    # was missing it. Symptom: Prod operator filled all 8 seats via
    # QSS, the UI showed every brain pill highlighted, but the
    # `SEAT REGISTRY DRIFT DETECTED — no executor assigned for
    # lane=crypto` banner never cleared because the diagnose
    # endpoint read `assignments.crypto` (None) while older roster
    # docs had `assignments.crypto_executor`. This migration drops
    # the legacy key on next read and persists the canonical one.
    "crypto_executor": "crypto",
}


def _canonical_role(role: str) -> str:
    """Rewrite legacy seat names to their canonical replacement.

    Boundary normalization: any API request, alias resolution, or stored
    document key for `decider` is silently rewritten to `strategist`
    (and `crypto_decider` → `crypto_strategist`). Returns the input
    unchanged for canonical names and unknown values."""
    return _LEGACY_ROLE_REWRITES.get(role, role)


async def get_roster() -> dict:
    """Return the live roster doc, creating defaults on first read."""
    doc = await db[BRAIN_ROSTER].find_one({"_id": "current"}, {"_id": 0})
    if doc:
        # Backfill seat_epoch on legacy docs (one if missing).
        if "seat_epoch" not in doc:
            doc["seat_epoch"] = 1
        assignments = doc.get("assignments") or {}
        # Migration (2026-05-24): rewrite legacy role keys to canonical
        # names. `decider → strategist`, `crypto_decider → crypto_strategist`.
        # If both old and new keys exist, the canonical key wins; the
        # legacy key is dropped. Persist the rewrite once.
        dirty = False
        for legacy, canonical in _LEGACY_ROLE_REWRITES.items():
            if legacy in assignments:
                if canonical not in assignments or assignments.get(canonical) is None:
                    assignments[canonical] = assignments[legacy]
                del assignments[legacy]
                dirty = True
        # Backfill new roles on legacy docs so adding ROLES later doesn't
        # leave the assignments dict missing keys. Missing role → vacant.
        missing = [r for r in ROLES if r not in assignments]
        if missing:
            for r in missing:
                assignments[r] = None
            dirty = True
        # ── Paradox v2 doctrine note (2026-02-19) ──
        # The crypto seat intentionally remains vacant by default.
        # Restrictions and trust lists belong to the SEAT, not the
        # brain — we never auto-seat a brain just to "unblock" the
        # lane. Lane unblocking is the seat's own responsibility via
        # its trust list + autonomy state (observe → shadow →
        # toehold → auto_execute). Any previous backfill was reverted.
        if dirty:
            doc["assignments"] = assignments
            await db[BRAIN_ROSTER].update_one(
                {"_id": "current"},
                {"$set": {"assignments": assignments}},
            )
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
        # Seed each row from the operator's default doctrine (NOT
        # all-true) so newly-added roles inherit the same lockdown
        # rules as the existing seats. The persisted matrix overlays
        # this default for any cell the operator has explicitly toggled.
        matrix = {b: dict(DEFAULT_ELIGIBILITY[b]) for b in BRAINS}
        for brain, roles in doc["matrix"].items():
            if brain not in matrix:
                continue
            for role, allowed in (roles or {}).items():
                # Rewrite legacy `decider` keys to canonical `strategist`.
                canonical = _canonical_role(role)
                if canonical in matrix[brain]:
                    matrix[brain][canonical] = bool(allowed)
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
    the eligibility matrix. Vacating (brain=None) is always allowed.

    Hard doctrine guard (2026-05-26): the governor + crypto_governor
    seats are exclusive to Chevelle and RedEye. Refuse any assignment
    that would put alpha or camaro into either seat BEFORE consulting
    the stored matrix — this defends against a stale or corrupted
    matrix doc."""
    if brain is None:
        return
    role = _canonical_role(role)
    if (
        role in _GOVERNOR_EXCLUSIVE_SEATS
        and brain not in _GOVERNOR_EXCLUSIVE_BRAINS
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                f"doctrine: the {role} seat is exclusive to "
                f"{', '.join(_GOVERNOR_EXCLUSIVE_BRAINS)}. "
                f"{brain} cannot occupy it."
            ),
        )
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
    "strategist", "executor", "governor", "advisor", "opponent", "auditor",
    "crypto",
    "crypto_advisor", "crypto_governor", "crypto_opponent",
    "crypto_strategist", "crypto_auditor",
    # ── Deprecated aliases — accepted at ingress, rewritten to canonical ──
    # 2026-05-24: `decider` renamed to `strategist`. Old sidecars may
    # still post the legacy names; we rewrite at the boundary.
    "decider", "crypto_decider",
]
BrainT = Literal["camino", "barracuda", "hellcat", "gto"]


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


CRYPTO_LANE_ROLES = (
    "crypto", "crypto_advisor", "crypto_governor", "crypto_opponent",
    "crypto_strategist", "crypto_auditor",
    # Legacy: rewritten by alias table before this check is meaningful.
    "crypto_decider",
)


def _lane_of_role(role: str) -> str:
    """Two doctrinal lanes: equity (default) and crypto. Lane-isolated
    multi-seating: one brain can hold one role per lane simultaneously."""
    return "crypto" if role in CRYPTO_LANE_ROLES else "equity"


async def _wipe_legacy_executor_doc(actor: str, reason: str) -> None:
    """Auto-wipe the legacy `shared_executor_seat` document whenever
    the roster's `executor` seat is reassigned.

    Why this exists (2026-02-19 doctrine cleanup):
      MC used to keep two storage locations for the equity executor —
      the new roster (`shared_brain_roster.assignments.executor`) and
      the legacy single-row doc (`shared_executor_seat`). After the
      2026-02-17 fix the gate prefers roster, but the legacy doc kept
      its old value until manually cleared, which lit up the
      "SEAT REGISTRY DRIFT DETECTED" banner on the Intents page
      after every roster assignment.

      Clearing the legacy doc on every roster write makes the roster
      the single source of truth. `shared_executor_seat.get_seat_holder`
      falls through to the roster when the legacy doc holder is None.
      No downstream behavior change — the gate already prefers roster.
    """
    try:
        await db[SHARED_EXECUTOR_SEAT].update_one(
            {"_id": "executor"},
            {
                "$set": {
                    "holder": None,
                    "since": None,
                    "assigned_by": actor,
                    "reason": f"auto-cleared by roster write ({reason})",
                    "auto_cleared_at": _now_iso(),
                },
            },
            upsert=True,
        )
    except Exception:  # noqa: BLE001
        # Best-effort. The gate already prefers roster, so even if this
        # write fails the seat-registry-diagnose banner will be the
        # only victim — never a real authority drift.
        pass


@router.post("/assign")
async def assign(body: AssignIn, user: dict = Depends(get_current_user)):
    # Legacy role names (`decider`, `crypto_decider`) are rewritten to
    # their canonical replacement before any policy / eligibility check.
    target_role = _canonical_role(body.role)
    # Eligibility gate — refuse to place a brain in a seat the operator
    # has marked disallowed.
    await _ensure_assignment_eligible(target_role, body.brain)

    r = await get_roster()
    prev = dict(r["assignments"])
    new_assignments = dict(prev)

    # Doctrine (2026-02-15): one seat per brain WITHIN a lane. Multiple
    # seats per brain ACROSS lanes. Chevelle can hold equity governor AND
    # crypto_governor simultaneously — they're isolated councils.
    # Cross-lane multi-seating preserves the check-and-balance doctrine
    # without forcing the operator to choose between lanes.
    if body.brain:
        target_lane = _lane_of_role(target_role)
        for role, occupant in list(new_assignments.items()):
            if occupant == body.brain and role != target_role and _lane_of_role(role) == target_lane:
                # Same brain, same lane, different role — vacate.
                new_assignments[role] = None

    new_assignments[target_role] = body.brain

    if new_assignments == prev:
        # No-op assignment — don't audit-log it. BUT: if the operator
        # is touching the executor seat (even idempotently, e.g.
        # clicking the same brain pill on Quick Seat Switches to
        # refresh state), still fire the legacy-doc auto-wipe.
        # That click is the operator's explicit "make this the
        # truth right now" signal, and they often use it specifically
        # to clear the SEAT REGISTRY DRIFT banner.
        if target_role == "executor":
            actor_noop = user.get("email") or "operator"
            await _wipe_legacy_executor_doc(
                actor_noop,
                reason=f"roster assign (no-op refresh) executor={body.brain}",
            )
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
        "role": target_role,
        "from": prev.get(target_role),
        "to": body.brain,
        "before": prev,
        "after": new_assignments,
        "seat_epoch": new_epoch,
    })
    # Doctrine cleanup: if this assignment touched the equity executor
    # seat (either directly or as a same-lane vacate side-effect),
    # also clear the legacy `shared_executor_seat` doc so the
    # diagnose banner stays silent and the roster is single-source.
    if (
        target_role == "executor"
        or prev.get("executor") != new_assignments.get("executor")
    ):
        await _wipe_legacy_executor_doc(
            actor, reason=f"roster assign {target_role}={body.brain}",
        )
    # 2026-02-20: mirror executor changes to the Paradox v2 trust list
    # so the unified pipeline (UNIFIED_PIPELINE_ENABLED=true) sees the
    # same holder the operator just assigned via QSS. Non-executor
    # seats (strategist/governor/auditor) are skipped — they don't
    # gate broker calls and don't need a trust row.
    try:
        from shared.seat_state import mirror_executor_to_v2_trust  # noqa: WPS433
        for role_key in ("executor", "crypto"):
            if prev.get(role_key) != new_assignments.get(role_key):
                await mirror_executor_to_v2_trust(
                    role_key, new_assignments.get(role_key),
                )
    except Exception:  # noqa: BLE001
        # Best-effort. The legacy roster is still the source of truth;
        # a mirror failure only delays unified pipeline visibility.
        pass
    return await get_current(user)


@router.post("/swap")
async def swap(body: SwapIn, user: dict = Depends(get_current_user)):
    # Rewrite legacy role names at the boundary.
    role_a = _canonical_role(body.role_a)
    role_b = _canonical_role(body.role_b)
    if role_a == role_b:
        raise HTTPException(status_code=422, detail="role_a and role_b must differ")
    r = await get_roster()
    prev = dict(r["assignments"])
    # Eligibility gate — the brain moving INTO role_a must be eligible
    # for role_a, and vice versa for role_b. Vacant moves are fine.
    await _ensure_assignment_eligible(role_a, prev.get(role_b))
    await _ensure_assignment_eligible(role_b, prev.get(role_a))
    new_assignments = dict(prev)
    new_assignments[role_a], new_assignments[role_b] = (
        prev.get(role_b),
        prev.get(role_a),
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
        "role_a": role_a,
        "role_b": role_b,
        "before": prev,
        "after": new_assignments,
        "seat_epoch": new_epoch,
    })
    if (role_a == "executor" or role_b == "executor"
            or prev.get("executor") != new_assignments.get("executor")):
        await _wipe_legacy_executor_doc(
            actor, reason=f"roster swap {role_a}<->{role_b}",
        )
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
    await _wipe_legacy_executor_doc(actor, reason="roster reset to defaults")
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
    target_role = _canonical_role(body.role)

    # ── Doctrine guard: governor exclusivity (2026-05-26) ──
    # The governor / crypto_governor seats are EXCLUSIVE to Chevelle
    # and RedEye. The operator cannot grant either to alpha or camaro
    # via the eligibility endpoint — that's a hard doctrine line, not
    # a soft default. Tightening the cell (allowed=False) is always
    # permitted; loosening it for a non-eligible brain is refused.
    if (
        target_role in _GOVERNOR_EXCLUSIVE_SEATS
        and body.brain not in _GOVERNOR_EXCLUSIVE_BRAINS
        and body.allowed is True
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                f"doctrine: the {target_role} seat is exclusive to "
                f"{', '.join(_GOVERNOR_EXCLUSIVE_BRAINS)}. "
                f"{body.brain} cannot be granted eligibility for it."
            ),
        )

    matrix = await get_eligibility()
    current_value = matrix.get(body.brain, {}).get(target_role, False)
    if current_value == body.allowed:
        return await get_eligibility_matrix(user)

    # Safety: if disallowing a brain from a role they CURRENTLY occupy,
    # refuse — operator must vacate or swap first. This avoids the
    # confusing state of "brain X is in role Y but matrix says no".
    if not body.allowed:
        roster = await get_roster()
        if roster["assignments"].get(target_role) == body.brain:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"cannot disallow {body.brain} from {target_role} while "
                    f"they currently occupy that seat. Vacate or swap first."
                ),
            )

    matrix[body.brain][target_role] = body.allowed
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
        "role": target_role,
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

    # Walk the audit log NEWEST → OLDEST so the most recent transition
    # for each role is the first hit we record. We then short-circuit
    # the inner loop as soon as we've located the latest entry-into-role
    # event for the current occupant — that's the canonical seat-start.
    #
    # Doctrine fix (2026-02-19): previously this walked oldest→newest
    # with `.to_list(2000)`. Once the audit log grew past 2000 rows the
    # query truncated, dropping the MOST RECENT entries — which is
    # exactly the ones we need. After a swap, the tenure endpoint
    # returned stale `days_in_role` because the swap itself was
    # outside the returned window.
    log = await db[ROSTER_AUDIT_LOG].find(
        {}, {"_id": 0}
    ).sort("ts", -1).to_list(5000)

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
        for entry in log:  # newest → oldest
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
                # Newest-first walk → first hit is the canonical one.
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
