"""Learning Ladder — Phase 3 of ladder doctrine.

Doctrine pin (2026-02-18):

State per (brain, lane) tracking promotion progress along the ladder:

    observation_only  →  micro_paper  →  micro_live  →  normal_live

Default state for any (brain, lane): `observation_only`.

Transitions (operator may also manually promote / demote at any time;
all changes audit-logged):

    observation_only → micro_paper:
        ≥ 100 RESOLVED observation receipts
        AND win_rate > 0.55 (excluding "anchor_missing" / neutrals)

    micro_paper → micro_live:
        ≥ 50 micro-paper FILLS (real Alpaca paper receipts tagged
        execution_mode="ladder_paper")
        AND expectancy_R > 0.30

    micro_live → normal_live:
        operator decision only (live-money progression must be
        deliberate; no automatic promotion).

This module is a COUNTER + STATE TRACKER + Phase 4 AUTHORITY.
Phase 4 ENGAGED (2026-02-17): the sizing gate
(`shared/sizing_gate.evaluate_sizing_with_ladder`) reads this state
and clamps notional + routes (observe → paper → live_micro →
live_normal). The auto-router consults the sizing gate BEFORE the
advisory_only classifier so the ladder owns capital deployment.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from auth import get_current_user
from db import db
from namespaces import (
    EXECUTION_RECEIPTS,
    LEARNING_LADDER,
    LEARNING_LADDER_AUDIT,
    OBSERVATION_RECEIPTS,
    RUNTIMES,
)


logger = logging.getLogger(__name__)


router = APIRouter(prefix="/admin/learning-ladder", tags=["learning-ladder"])


STAGES = ("observation_only", "micro_paper", "micro_live", "normal_live")

# Unlock thresholds — operator-locked doctrine values.
OBS_UNLOCK_COUNT = 100
OBS_UNLOCK_WIN_RATE = 0.55
PAPER_UNLOCK_COUNT = 50
PAPER_UNLOCK_EXPECTANCY_R = 0.30


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _key(brain: str, lane: str) -> dict:
    return {"brain": brain, "lane": lane}


# ─────────────────────────── state ───────────────────────────


async def get_stage(brain: str, lane: str) -> dict:
    """Return the current ladder state for (brain, lane). Materializes
    `observation_only` if no row exists yet."""
    doc = await db[LEARNING_LADDER].find_one(_key(brain, lane), {"_id": 0})
    if not doc:
        return {
            "brain": brain, "lane": lane,
            "stage": "observation_only",
            "promoted_at": None, "promoted_by": None,
            "demoted_at": None, "demoted_by": None,
            "auto_promotable": False,
            "created": False,
        }
    return {**doc, "created": True}


async def _set_stage(brain: str, lane: str, stage: str, actor: str,
                     reason: str) -> dict:
    """Internal: write the new stage and audit-log the transition."""
    if stage not in STAGES:
        raise ValueError(f"unknown stage {stage!r}; must be one of {STAGES}")
    now = _now_iso()
    prev = await db[LEARNING_LADDER].find_one(_key(brain, lane), {"_id": 0})
    prev_stage = (prev or {}).get("stage", "observation_only")

    field_updates = {
        "brain": brain, "lane": lane,
        "stage": stage,
        "updated_at": now,
        "updated_by": actor,
    }
    is_promotion = STAGES.index(stage) > STAGES.index(prev_stage)
    is_demotion = STAGES.index(stage) < STAGES.index(prev_stage)
    if is_promotion:
        field_updates["promoted_at"] = now
        field_updates["promoted_by"] = actor
    elif is_demotion:
        field_updates["demoted_at"] = now
        field_updates["demoted_by"] = actor

    await db[LEARNING_LADDER].update_one(
        _key(brain, lane),
        {"$set": field_updates,
         "$setOnInsert": {"created_at": now}},
        upsert=True,
    )
    await db[LEARNING_LADDER_AUDIT].insert_one({
        "ts": now,
        "brain": brain,
        "lane": lane,
        "previous": prev_stage,
        "next": stage,
        "actor": actor,
        "reason": reason,
    })
    logger.info(
        "learning_ladder: %s/%s %s → %s by %s (%s)",
        brain, lane, prev_stage, stage, actor, reason,
    )
    return await get_stage(brain, lane)


# ─────────────────────────── progress math ───────────────────────────


async def _obs_progress(brain: str, lane: str) -> dict:
    """Resolved observation receipts + win rate for this (brain, lane)."""
    q = {"brain": brain, "lane": lane, "resolved": True,
         "outcome": {"$in": ["win", "loss", "neutral"]}}
    total = await db[OBSERVATION_RECEIPTS].count_documents(q)
    wins = await db[OBSERVATION_RECEIPTS].count_documents(
        {**q, "outcome": "win"})
    decisive = await db[OBSERVATION_RECEIPTS].count_documents(
        {**q, "outcome": {"$in": ["win", "loss"]}})
    win_rate = (wins / decisive) if decisive else None
    threshold_met = (
        total >= OBS_UNLOCK_COUNT
        and win_rate is not None
        and win_rate > OBS_UNLOCK_WIN_RATE
    )
    return {
        "resolved": total,
        "wins": wins,
        "win_rate": round(win_rate, 4) if win_rate is not None else None,
        "unlock_count": OBS_UNLOCK_COUNT,
        "unlock_win_rate": OBS_UNLOCK_WIN_RATE,
        "threshold_met": threshold_met,
        "progress_pct": min(100.0, round(total / OBS_UNLOCK_COUNT * 100, 1)),
    }


async def _paper_progress(brain: str, lane: str) -> dict:
    """Micro-paper fills + R-expectancy for this (brain, lane).
    Reads `execution_receipts` filtered to ladder-paper mode.

    Phase 4 will tag receipts `execution_mode="ladder_paper"`.

    2026-02-17: Phase 4 IS NOW ENGAGED. Receipts written by the
    auto-router at stage=micro_paper carry
    `execution_mode="ladder_paper"` (via sizing_gate). This counter
    now ticks as soon as the operator promotes a (brain, lane) to
    micro_paper and the first signal fires."""
    q = {
        "brain": brain, "lane": lane,
        "execution_mode": "ladder_paper",
        "resolved": True,
    }
    fills = await db[EXECUTION_RECEIPTS].count_documents(q)
    # Expectancy_R: avg of (pnl_R) across fills.
    pipeline = [
        {"$match": q},
        {"$group": {"_id": None, "avg_R": {"$avg": "$pnl_R"}}},
    ]
    rows = await db[EXECUTION_RECEIPTS].aggregate(pipeline).to_list(1)
    avg_R = (rows[0]["avg_R"] if rows else None)
    threshold_met = (
        fills >= PAPER_UNLOCK_COUNT
        and avg_R is not None
        and avg_R > PAPER_UNLOCK_EXPECTANCY_R
    )
    return {
        "fills": fills,
        "expectancy_R": round(avg_R, 4) if avg_R is not None else None,
        "unlock_count": PAPER_UNLOCK_COUNT,
        "unlock_expectancy_R": PAPER_UNLOCK_EXPECTANCY_R,
        "threshold_met": threshold_met,
        "progress_pct": min(100.0, round(fills / PAPER_UNLOCK_COUNT * 100, 1)),
    }


async def _next_stage_eligibility(brain: str, lane: str, stage: str) -> dict:
    """Return whether (brain, lane) at `stage` is eligible to auto-
    promote to the next rung."""
    if stage == "observation_only":
        prog = await _obs_progress(brain, lane)
        return {
            "next": "micro_paper",
            "progress": prog,
            "auto_promotable": prog["threshold_met"],
        }
    if stage == "micro_paper":
        prog = await _paper_progress(brain, lane)
        return {
            "next": "micro_live",
            "progress": prog,
            "auto_promotable": prog["threshold_met"],
        }
    if stage == "micro_live":
        return {
            "next": "normal_live",
            "progress": {"note": "operator decision required; no auto-promote"},
            "auto_promotable": False,
        }
    return {
        "next": None,
        "progress": {"note": "at top rung"},
        "auto_promotable": False,
    }


# ─────────────────────────── routes ───────────────────────────


@router.get("")
async def list_ladder(_user: dict = Depends(get_current_user)):  # noqa: B008
    """All brain × lane combinations with current stage + progress."""
    rows = []
    for brain in RUNTIMES:
        for lane in ("equity", "crypto"):
            state = await get_stage(brain, lane)
            eligibility = await _next_stage_eligibility(brain, lane, state["stage"])
            rows.append({**state, **eligibility})
    return {
        "items": rows,
        "doctrine": {
            "stages": list(STAGES),
            "observation_unlock_count": OBS_UNLOCK_COUNT,
            "observation_unlock_win_rate": OBS_UNLOCK_WIN_RATE,
            "paper_unlock_count": PAPER_UNLOCK_COUNT,
            "paper_unlock_expectancy_R": PAPER_UNLOCK_EXPECTANCY_R,
            "note": (
                "Phase 4 ENGAGED (2026-02-17). The ladder stage now "
                "drives sizing + routing via "
                "shared.sizing_gate.evaluate_sizing_with_ladder. "
                "Stage observation_only → observation receipt only "
                "(no broker fill, even if the brain sized > 0); "
                "micro_paper → paper fire @ LADDER_MICRO_PAPER_USD; "
                "micro_live → live fire @ LADDER_MICRO_LIVE_USD; "
                "normal_live → full sizing. Promotions are deliberate; "
                "all transitions audit-logged."
            ),
        },
    }


class StageActionIn(BaseModel):
    brain: str = Field(...)
    lane: Literal["equity", "crypto"]
    reason: str = Field("operator_action", max_length=500)


@router.post("/promote")
async def promote_stage(
    body: StageActionIn,
    user: dict = Depends(get_current_user),  # noqa: B008
):
    """Promote (brain, lane) one rung. Auto if eligible; operator-
    forced otherwise. Promotions are NOT auto-executed by the
    learning ladder — they require this explicit call. This keeps
    capital-risk transitions deliberate."""
    if body.brain not in RUNTIMES:
        raise HTTPException(status_code=400, detail=f"unknown brain {body.brain!r}")
    state = await get_stage(body.brain, body.lane)
    cur = state["stage"]
    if cur not in STAGES[:-1]:
        raise HTTPException(status_code=400, detail=f"already at top stage {cur!r}")
    next_stage = STAGES[STAGES.index(cur) + 1]
    actor = user.get("email") or "operator"
    new_state = await _set_stage(body.brain, body.lane, next_stage, actor, body.reason)
    return {"ok": True, "previous": cur, "current": next_stage,
            "state": new_state}


@router.post("/demote")
async def demote_stage(
    body: StageActionIn,
    user: dict = Depends(get_current_user),  # noqa: B008
):
    """Demote (brain, lane) one rung. Always allowed by the operator —
    demotion is a safety operation, never blocked."""
    if body.brain not in RUNTIMES:
        raise HTTPException(status_code=400, detail=f"unknown brain {body.brain!r}")
    state = await get_stage(body.brain, body.lane)
    cur = state["stage"]
    if cur == STAGES[0]:
        raise HTTPException(status_code=400, detail=f"already at bottom stage {cur!r}")
    prev_stage = STAGES[STAGES.index(cur) - 1]
    actor = user.get("email") or "operator"
    new_state = await _set_stage(body.brain, body.lane, prev_stage, actor, body.reason)
    return {"ok": True, "previous": cur, "current": prev_stage,
            "state": new_state}


class StageSetIn(BaseModel):
    """Direct stage selection — operator picks any rung (jump or drop).

    Distinct from promote/demote, which step exactly one rung. This
    endpoint lets the UI render a toggle and let the operator pick
    e.g. `observation_only → micro_live` in a single click without
    chaining two promote calls. Every transition is still audit-
    logged with the operator's reason; no upgrade is silent.
    """
    brain: str = Field(...)
    lane: Literal["equity", "crypto"]
    stage: Literal["observation_only", "micro_paper", "micro_live", "normal_live"]
    reason: str = Field("operator_action", max_length=500)


@router.post("/set")
async def set_stage_route(
    body: StageSetIn,
    user: dict = Depends(get_current_user),  # noqa: B008
):
    """Operator-direct stage selection. Jumps or drops to ANY rung.

    Same write path as promote/demote (audit row + state upsert),
    so the history view shows every transition uniformly regardless
    of whether it came from /promote, /demote, or /set.
    """
    if body.brain not in RUNTIMES:
        raise HTTPException(status_code=400, detail=f"unknown brain {body.brain!r}")
    state = await get_stage(body.brain, body.lane)
    cur = state["stage"]
    if cur == body.stage:
        return {"ok": True, "previous": cur, "current": cur,
                "noop": True, "state": state}
    actor = user.get("email") or "operator"
    new_state = await _set_stage(
        body.brain, body.lane, body.stage, actor, body.reason,
    )
    return {"ok": True, "previous": cur, "current": body.stage,
            "noop": False, "state": new_state}


@router.get("/history")
async def ladder_history(
    limit: int = 100,
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    """Read-only audit log of every promotion/demotion."""
    rows = (
        await db[LEARNING_LADDER_AUDIT]
        .find({}, {"_id": 0})
        .sort("ts", -1)
        .to_list(min(max(limit, 1), 1000))
    )
    return {"items": rows, "count": len(rows)}
