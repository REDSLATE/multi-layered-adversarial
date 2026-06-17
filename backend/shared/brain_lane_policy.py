"""Per-brain × lane intent-emission policy — RETIRED.

Doctrine pin (2026-02-17, rev3):
    The brain-lane policy was the operator-controlled mute switch on
    intent emission. It was a brain-IDENTITY restriction surface, which
    the architecture now explicitly forbids: authority lives on SEATS,
    not on brains. Mute / unmute decisions are now expressed by VACATING
    a seat (no holder = no authority for that role), not by flagging a
    brain.

    This module is preserved as a no-op shim so any legacy import path
    keeps working:
      • `is_brain_lane_allowed()` always returns True.
      • The persisted `brain_lane_policy` collection is no longer read
        by the gate, only retained for audit-history reference.
      • `seed_default_policy()` is a no-op.
      • The router still mounts (so the deployed frontend bundle
        doesn't 404 if it asks) but the POST endpoint refuses any
        non-`allowed=true` write and returns a doctrine-pinned error
        message.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from auth import get_current_user
from db import db
from namespaces import BRAIN_LANE_POLICY


KNOWN_BRAINS = ("camino", "barracuda", "hellcat", "gto")
KNOWN_LANES = ("equity", "crypto")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def is_brain_lane_allowed(brain: str, lane: Optional[str]) -> bool:
    """Doctrine pin (2026-02-17, rev3): authority is seat-bound, not
    brain-bound. This check is permanently True — no brain can be
    muted by identity. To stop a brain from emitting in a lane,
    vacate the seat it would be holding.
    """
    return True


async def get_policy_snapshot() -> list[dict]:
    """Retained for historical visibility. Returns whatever rows the
    collection still contains; readers must understand these rows are
    AUDIT-ONLY and do not gate anything anymore."""
    rows = await db[BRAIN_LANE_POLICY].find({}, {"_id": 0}).to_list(200)
    rows.sort(key=lambda r: (r.get("brain", ""), r.get("lane", "")))
    return rows


async def seed_default_policy() -> None:
    """No-op (was idempotent allow-seeding). Retained so server.py's
    lifespan call doesn't need to change.

    Doctrine pin (2026-02-17, rev3): we additionally PURGE any
    legacy `allowed=false` rows on boot so the persisted DB state can
    never re-introduce a brain-identity block. This is a one-time
    cleanup; subsequent boots find zero rows to purge.
    """
    try:
        await db[BRAIN_LANE_POLICY].delete_many({"allowed": False})
    except Exception:
        pass
    return None


# ─────────────────────────── REST surface ───────────────────────────

router = APIRouter(prefix="/admin/brain-lane-policy", tags=["roster"])


class PolicyIn(BaseModel):
    brain: Literal["camino", "barracuda", "hellcat", "gto"]
    lane: Literal["equity", "crypto"]
    allowed: bool
    reason: Optional[str] = Field(default=None, max_length=512)


@router.get("")
async def list_policy(_user: dict = Depends(get_current_user)):  # noqa: B008
    """Returns the historical policy rows (audit-only) + an effective
    matrix that is ALWAYS allowed=True per the seat-doctrine.
    """
    rows = await get_policy_snapshot()
    effective: dict = {}
    for b in KNOWN_BRAINS:
        effective[b] = {}
        for la in KNOWN_LANES:
            effective[b][la] = True  # doctrine pin: always allowed
    return {
        "items": rows,
        "effective": effective,
        "doctrine": (
            "Brain × lane policy is RETIRED. Authority lives on seats, "
            "not on brain identity. To stop a brain from acting, vacate "
            "its seat — do not mute by name."
        ),
    }


@router.post("")
async def set_policy(
    body: PolicyIn,
    user: dict = Depends(get_current_user),  # noqa: B008
):
    """Doctrine pin (2026-02-17, rev3): this endpoint no longer accepts
    `allowed=false`. A POST that tries to mute a brain returns 410
    Gone with a doctrine-pinned explanation. Allow-toggles are silently
    accepted and recorded so the audit trail stays continuous.
    """
    if not body.allowed:
        raise HTTPException(
            status_code=410,
            detail=(
                "Brain × lane mute is retired. Authority lives on SEATS, "
                "not on brain identity. To stop a brain from acting in a "
                "lane, vacate its seat in /admin/roster instead."
            ),
        )
    await db[BRAIN_LANE_POLICY].update_one(
        {"brain": body.brain, "lane": body.lane},
        {"$set": {
            "brain": body.brain,
            "lane": body.lane,
            "allowed": True,
            "reason": "explicit allow (audit only)",
            "set_by": user.get("email") or "operator",
            "set_at": _now_iso(),
        }},
        upsert=True,
    )
    return await list_policy(_user=user)


@router.delete("/{brain}/{lane}")
async def clear_policy(
    brain: Literal["camino", "barracuda", "hellcat", "gto"],
    lane: Literal["equity", "crypto"],
    user: dict = Depends(get_current_user),  # noqa: B008
):
    """Remove the policy row. Always returns 200 (idempotent); a
    missing row is fine because the effective state is always allow."""
    await db[BRAIN_LANE_POLICY].delete_one({"brain": brain, "lane": lane})
    return await list_policy(_user=user)
