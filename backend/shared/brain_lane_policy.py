"""Per-brain × lane intent-emission policy.

Doctrine (2026-02-16):
    Independent of the seat eligibility matrix. Eligibility governs
    WHICH SEATS a brain may hold; this module governs whether a brain
    may even *post* an intent for a given lane.

    Setting `{brain: "camaro", lane: "crypto", allowed: false}` causes
    `POST /api/intents` and `POST /api/admin/intents` to reject Camaro's
    crypto intents with HTTP 403 — the intent never enters
    `shared_intents` at all. Useful when an engine is misbehaving and
    the operator wants to mute it without touching the sidecar.

    Default policy: allow. Only explicit `allowed=false` rows block.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from auth import get_current_user
from db import db
from namespaces import BRAIN_LANE_POLICY


KNOWN_BRAINS = ("alpha", "camaro", "chevelle", "redeye")
KNOWN_LANES = ("equity", "crypto")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def is_brain_lane_allowed(brain: str, lane: Optional[str]) -> bool:
    """Returns False ONLY if there's an explicit `allowed=false` row
    for this (brain, lane) pair. Missing lanes (None) always pass —
    the lane-inference layer or downstream gates handle those."""
    if not brain or not lane:
        return True
    row = await db[BRAIN_LANE_POLICY].find_one(
        {"brain": brain.lower(), "lane": lane.lower()},
        {"_id": 0, "allowed": 1},
    )
    if row is None:
        return True
    return bool(row.get("allowed", True))


async def get_policy_snapshot() -> list[dict]:
    """All explicit policy rows. Sorted brain → lane for UI."""
    rows = await db[BRAIN_LANE_POLICY].find({}, {"_id": 0}).to_list(200)
    rows.sort(key=lambda r: (r.get("brain", ""), r.get("lane", "")))
    return rows


async def seed_default_policy() -> None:
    """Idempotent. Called from server.py lifespan on boot.

    Doctrine (2026-02-16): Camaro is the equity decider and the
    crypto OPPONENT. It voices crypto setups for REDEYE (crypto
    executor) to evaluate — it does not emit crypto intents directly.
    Until REDEYE's crypto pipeline is online, Camaro's crypto intents
    were stacking up in `pending` because they fail the
    executor_seat_check gate every time. Blocking them at ingest is
    cleaner — they never enter `shared_intents` at all.
    """
    await db[BRAIN_LANE_POLICY].update_one(
        {"brain": "camaro", "lane": "crypto"},
        {"$setOnInsert": {
            "brain": "camaro",
            "lane": "crypto",
            "allowed": False,
            "reason": (
                "Camaro is crypto_opponent, not crypto_executor. Per doctrine"
                " (2026-02-16), opponent brains voice setups but do not emit"
                " execution intents. Re-enable once Camaro holds the crypto"
                " seat OR the seat-endorsement doctrine is in place."
            ),
            "set_by": "system_seed",
            "set_at": _now_iso(),
        }},
        upsert=True,
    )


# ─────────────────────────── REST surface ───────────────────────────

router = APIRouter(prefix="/admin/brain-lane-policy", tags=["roster"])


class PolicyIn(BaseModel):
    brain: Literal["alpha", "camaro", "chevelle", "redeye"]
    lane: Literal["equity", "crypto"]
    allowed: bool
    reason: Optional[str] = Field(default=None, max_length=512)


@router.get("")
async def list_policy(_user: dict = Depends(get_current_user)):  # noqa: B008
    rows = await get_policy_snapshot()
    # Plus an "effective matrix" view: every (brain, lane) cell with its
    # resolved allowed value — defaults to True if no explicit row.
    effective: dict = {}
    for b in KNOWN_BRAINS:
        effective[b] = {}
        for la in KNOWN_LANES:
            effective[b][la] = await is_brain_lane_allowed(b, la)
    return {
        "items": rows,
        "effective": effective,
        "doctrine": (
            "Per-brain × lane intent-emission policy. Independent of seat"
            " eligibility — controls whether a brain may even POST an"
            " intent for a given lane. Default: allow."
        ),
    }


@router.post("")
async def set_policy(
    body: PolicyIn,
    user: dict = Depends(get_current_user),  # noqa: B008
):
    await db[BRAIN_LANE_POLICY].update_one(
        {"brain": body.brain, "lane": body.lane},
        {"$set": {
            "brain": body.brain,
            "lane": body.lane,
            "allowed": body.allowed,
            "reason": body.reason,
            "set_by": user.get("email") or "operator",
            "set_at": _now_iso(),
        }},
        upsert=True,
    )
    return await list_policy(_user=user)


@router.delete("/{brain}/{lane}")
async def clear_policy(
    brain: Literal["alpha", "camaro", "chevelle", "redeye"],
    lane: Literal["equity", "crypto"],
    user: dict = Depends(get_current_user),  # noqa: B008
):
    """Remove the policy row — falls back to the default (allow)."""
    r = await db[BRAIN_LANE_POLICY].delete_one({"brain": brain, "lane": lane})
    if r.deleted_count == 0:
        raise HTTPException(status_code=404, detail=f"no policy for {brain}/{lane}")
    return await list_policy(_user=user)
