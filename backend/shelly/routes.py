"""Shelly admin endpoints — operator-visible memory + rollup surface.
SYNC handlers (FastAPI runs `def` routes in the threadpool).

Endpoints:
    POST /api/admin/shelly/rollup     — trigger MC rollup now (idempotent)
    GET  /api/admin/shelly/status     — per-brain Shelly counts
    POST /api/admin/shelly/reason     — operator reasoning probe

Doctrine pin: every response carries `authority="memory_reasoning_only"`.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from auth import get_current_user
from namespaces import LIVE_RUNTIMES
from shelly.contracts import (
    AUTHORITY_MEMORY_REASONING_ONLY,
    RECOMMENDATIONS_ALLOWED,
    RECOMMENDATIONS_BANNED,
)
from shelly.pipeline import shelly_pipeline
from shelly.sync_db import get_db


router = APIRouter(tags=["admin", "shelly"])


class ReasonProbeBody(BaseModel):
    symbol: str = Field(..., min_length=1, max_length=32)
    direction: str = Field(..., min_length=1, max_length=16)
    brain: Optional[str] = Field(
        default=None,
        description="if provided, also run that brain's LocalShelly reasoning",
    )


class SimilarProbeBody(BaseModel):
    brain: str = Field(..., min_length=1, max_length=32)
    symbol: str = Field(..., min_length=1, max_length=32)
    direction: str = Field(..., min_length=1, max_length=16)
    features: Optional[dict[str, Any]] = Field(default=None)
    top_k: int = Field(default=10, ge=1, le=50)
    min_score: float = Field(default=0.0, ge=0.0, le=1.0)


@router.post("/admin/shelly/rollup")
def trigger_rollup(user: dict = Depends(get_current_user)):  # noqa: B008
    """Drain every LocalShelly's un-rolled memories into MCShelly.
    Idempotent — re-running after completion returns zeros."""
    result = shelly_pipeline.rollup_all_to_mc()
    return {
        **result,
        "triggered_by": user.get("email"),
        "doctrine_note": (
            "Rollup is memory-only. No seats, gates, or routing "
            "affected. Idempotent — safe to re-run."
        ),
    }


@router.get("/admin/shelly/status")
def shelly_status(_user: dict = Depends(get_current_user)):  # noqa: B008
    """Per-brain Shelly counts + last activity. Read-only."""
    db = get_db()
    locals_status: dict[str, dict[str, Any]] = {}
    for brain, shelly in shelly_pipeline.locals.items():
        memory_count = db[shelly.memories_coll_name].count_documents({})
        receipt_count = db[shelly.receipts_coll_name].count_documents({})
        unrolled = db[shelly.memories_coll_name].count_documents(
            {"rolled_to_mc": {"$ne": True}},
        )
        latest_receipt = db[shelly.receipts_coll_name].find_one(
            {}, {"_id": 0, "created_at": 1, "recommendation": 1, "symbol": 1},
            sort=[("created_at", -1)],
        )
        locals_status[brain] = {
            "memory_count": memory_count,
            "receipt_count": receipt_count,
            "unrolled_memories": unrolled,
            "latest_receipt": latest_receipt,
        }

    mc = shelly_pipeline.mc_shelly
    mc_memory_count = db[mc.SHARED_MEMORY_COLL].count_documents({})
    mc_receipt_count = db[mc.RECEIPTS_COLL].count_documents({})
    mc_latest = db[mc.RECEIPTS_COLL].find_one(
        {}, {"_id": 0, "created_at": 1, "recommendation": 1,
             "symbol": 1, "has_brain_conflict": 1},
        sort=[("created_at", -1)],
    )

    return {
        "live_runtimes": list(LIVE_RUNTIMES),
        "locals": locals_status,
        "mc_shelly": {
            "shared_memory_count": mc_memory_count,
            "receipt_count": mc_receipt_count,
            "latest_receipt": mc_latest,
        },
        "vocabulary": {
            "allowed": sorted(RECOMMENDATIONS_ALLOWED),
            "banned": sorted(RECOMMENDATIONS_BANNED),
        },
        "authority": AUTHORITY_MEMORY_REASONING_ONLY,
        "doctrine_note": (
            "Shelly is memory + reasoning ONLY. Brain decides; MC "
            "verifies; RoadGuard guards safety. Shelly does not "
            "execute, block, override, or promote."
        ),
    }


@router.post("/admin/shelly/reason")
def reason_probe(
    body: ReasonProbeBody,
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    """Operator-driven reasoning probe."""
    current_case = {
        "symbol": body.symbol.upper(),
        "direction": body.direction.upper(),
    }
    mc_reasoning = shelly_pipeline.mc_shelly.reason_across_shellys(
        current_case,
    )

    local_reasoning: Optional[dict[str, Any]] = None
    if body.brain:
        brain = body.brain.lower()
        local = shelly_pipeline.locals.get(brain)
        if local is not None:
            local_reasoning = local.reason(current_case)

    return {
        "case": current_case,
        "mc_reasoning": mc_reasoning,
        "local_reasoning": local_reasoning,
        "authority": AUTHORITY_MEMORY_REASONING_ONLY,
        "doctrine_note": (
            "Reasoning probe — informational only. The verdict "
            "carries no authority and does not modify any seat, "
            "gate, or execution decision."
        ),
    }



@router.post("/admin/shelly/find-similar")
def find_similar_probe(
    body: SimilarProbeBody,
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    """Phase 2 — semantic retrieval over a brain's memories.

    Doctrine: ADVISORY_ONLY. Returns ranked candidates with cosine
    `similarity` scores in [0, 1]. Carries no execution authority.
    """
    brain = body.brain.lower()
    local = shelly_pipeline.locals.get(brain)
    if local is None:
        return {
            "ok": False,
            "reason": "UNKNOWN_BRAIN",
            "brain": brain,
            "known_brains": sorted(shelly_pipeline.locals.keys()),
            "authority": AUTHORITY_MEMORY_REASONING_ONLY,
        }
    case: dict[str, Any] = {
        "symbol": body.symbol.upper(),
        "direction": body.direction.upper(),
    }
    if body.features:
        case["features"] = body.features
    matches = local.find_similar(
        case, top_k=body.top_k, min_score=body.min_score,
    )
    return {
        "ok": True,
        "brain": brain,
        "case": case,
        "matches": matches,
        "count": len(matches),
        "authority": AUTHORITY_MEMORY_REASONING_ONLY,
        "doctrine_note": (
            "Semantic retrieval — ADVISORY context. Brain decides; "
            "MC verifies; RoadGuard guards safety."
        ),
    }
