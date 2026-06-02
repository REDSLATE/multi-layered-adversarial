"""MC-Shelly ingest endpoint — receives brain memory proposals.

Wire path: `POST /api/mc-shelly/memory/propose`
  Header: `X-Runtime-Token: <brain ingest token>` (per-brain;
          must match {BRAIN}_INGEST_TOKEN env, like every other
          brain-side endpoint).
  Body:   `ShellyMemoryProposal` JSON.

Trust scoring is intentionally simple. Operator can tune the
thresholds without code change via env:
    SHELLY_BUS_MIN_CANONICAL_TRUST  (default 0.75)
    SHELLY_BUS_VERIFIED_TRUST       (default 0.90)
    SHELLY_BUS_CONVERGED_TRUST      (default 0.80)
    SHELLY_BUS_UNVERIFIED_TRUST     (default 0.35)
    SHELLY_BUS_MIN_CONVERGENCE      (default 2)

A canonical accept (`trust ≥ MIN_CANONICAL_TRUST`) translates the
proposal into a `ShellyMemoryEvent` and ingests it through the
existing `MCShelly.ingest_rollup` path so the proposal merges into
the same `shelly_mc_shared_memory` collection MC already trusts.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from db import db
from runtime_auth import verify_runtime_token
from shelly.contracts import ShellyMemoryEvent
from shelly.mc_shelly import MCShelly
from shared.shelly_bus import (
    CANONICAL_AUTHORITY,
    PROPOSAL_AUTHORITY,
    REVIEW_AUTHORITY,
    utc_now,
)


logger = logging.getLogger("mc_shelly.ingest")


PROPOSALS_COLL = "shelly_memory_proposals"


def _f(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _i(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


MIN_CANONICAL_TRUST = _f("SHELLY_BUS_MIN_CANONICAL_TRUST", 0.75)
TRUST_VERIFIED = _f("SHELLY_BUS_VERIFIED_TRUST", 0.90)
TRUST_CONVERGED = _f("SHELLY_BUS_CONVERGED_TRUST", 0.80)
TRUST_UNVERIFIED = _f("SHELLY_BUS_UNVERIFIED_TRUST", 0.35)
MIN_CONVERGENCE = _i("SHELLY_BUS_MIN_CONVERGENCE", 2)


class ProposalIn(BaseModel):
    """HTTP body model — mirrors ShellyMemoryProposal but Pydantic so
    FastAPI handles validation."""
    source_brain: str
    lane: str
    symbol: str
    event_type: str
    text: str
    confidence: float = 0.0
    outcome: str | None = None
    regime: str | None = None
    source_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    authority: str = PROPOSAL_AUTHORITY


router = APIRouter(prefix="/mc-shelly", tags=["mc-shelly-bus"])


async def _trust_score(proposal: ProposalIn) -> tuple[float, str, dict[str, Any]]:
    """Return (trust_score, status, evidence) for a proposal."""
    # 1) verified_outcomes: if MC has already resolved an outcome for
    #    this source_id, the proposal lands at 0.90 (VERIFIED).
    if proposal.source_id:
        v = await db.shared_brain_outcomes.find_one(
            {"$or": [
                {"opinion_id": proposal.source_id},
                {"intent_id": proposal.source_id},
                {"source_id": proposal.source_id},
            ]},
            {"_id": 0, "outcome_status": 1},
        )
        if v:
            return TRUST_VERIFIED, "VERIFIED", {"verified_outcome_match": True}

    # 2) brain convergence: at least N OTHER brains have already
    #    submitted a proposal carrying the same (symbol, event_type,
    #    text). Defensive: don't count the same brain twice.
    if proposal.symbol and proposal.event_type:
        same_topic = db[PROPOSALS_COLL].distinct(
            "source_brain",
            {
                "symbol": proposal.symbol,
                "event_type": proposal.event_type,
                "text": proposal.text,
                "source_brain": {"$ne": proposal.source_brain.lower()},
            },
        )
        other_brains = await same_topic
        if isinstance(other_brains, list) and len(other_brains) + 1 >= MIN_CONVERGENCE:
            return TRUST_CONVERGED, "CONVERGED", {
                "convergence_n": len(other_brains) + 1,
                "other_brains": other_brains,
            }

    # 3) Default: unverified. Stays in the proposal pen.
    return TRUST_UNVERIFIED, "UNVERIFIED", {}


@router.post("/memory/propose")
async def propose_memory(
    payload: ProposalIn,
    x_runtime_token: str | None = Header(default=None, alias="X-Runtime-Token"),
) -> dict[str, Any]:
    """Brain submits a memory proposal. MC verifies, scores, decides."""
    # Auth: the X-Runtime-Token MUST match the brain claimed in
    # source_brain. Camaro can't impersonate RedEye even with a stolen
    # token of its own.
    verify_runtime_token(payload.source_brain.lower(), x_runtime_token or "")

    # Honest defense: the brain MUST stamp MEMORY_PROPOSAL_ONLY. If it
    # tries to claim a different authority we reject loudly (not silent
    # rewrite — that would mask a misbehaving brain).
    if payload.authority != PROPOSAL_AUTHORITY:
        raise HTTPException(
            status_code=400,
            detail=(
                f"brain must stamp authority={PROPOSAL_AUTHORITY!r}; "
                f"got {payload.authority!r}"
            ),
        )

    trust_score, status, evidence = await _trust_score(payload)

    # Always record the proposal — even rejected/unverified ones —
    # so the proposal pen is the audit trail.
    proposal_doc = {
        "source_brain": payload.source_brain.lower(),
        "lane": payload.lane,
        "symbol": payload.symbol,
        "event_type": payload.event_type,
        "text": payload.text,
        "confidence": float(payload.confidence),
        "outcome": payload.outcome,
        "regime": payload.regime,
        "source_id": payload.source_id,
        "metadata": payload.metadata or {},
        "status": status,
        "trust_score": float(trust_score),
        "trust_evidence": evidence,
        # Authority re-stamped here at the boundary. The brain's
        # claimed value is preserved above (rejected if wrong); this
        # is MC's verdict on what the row actually IS now.
        "authority": REVIEW_AUTHORITY,
        "created_at": utc_now(),
    }
    await db[PROPOSALS_COLL].insert_one(dict(proposal_doc))

    # Below the canonical threshold → stop here.
    if trust_score < MIN_CANONICAL_TRUST:
        return {
            "accepted": False,
            "status": status,
            "trust_score": trust_score,
            "reason": "stored as proposal, not canonical memory yet",
            "min_canonical_trust": MIN_CANONICAL_TRUST,
            "authority": REVIEW_AUTHORITY,
        }

    # Trust threshold met → translate to ShellyMemoryEvent and ingest
    # through MCShelly's existing rollup path so the row joins the
    # same `shelly_mc_shared_memory` everything else reads.
    event = ShellyMemoryEvent(
        brain=payload.source_brain.lower(),
        symbol=payload.symbol,
        direction=(payload.outcome or "HOLD").upper(),
        confidence=float(payload.confidence),
        decision=payload.event_type,
        features={
            "regime": payload.regime,
            "text": payload.text,
            "lane": payload.lane,
            **(payload.metadata or {}),
        },
        mc_status="VIA_SHELLY_BUS",
        outcome=({"pnl_pct": 0.0} if payload.outcome in ("win", "loss") else None),
    )
    mc = MCShelly()
    ingest = mc.ingest_rollup(
        brain=payload.source_brain.lower(),
        memories=[event.to_doc()],
    )

    return {
        "accepted": True,
        "status": status,
        "trust_score": trust_score,
        "memory_receipt": ingest,
        "authority": CANONICAL_AUTHORITY,
    }


@router.get("/memory/proposals/summary")
async def proposals_summary() -> dict[str, Any]:
    """Dashboard tile. Read-only count by status."""
    total = await db[PROPOSALS_COLL].count_documents({})
    pipeline = [
        {"$group": {"_id": "$status", "n": {"$sum": 1}}},
    ]
    by_status: list[dict[str, Any]] = []
    async for row in db[PROPOSALS_COLL].aggregate(pipeline):
        by_status.append({"status": row.get("_id") or "UNKNOWN", "n": row["n"]})
    return {
        "ok": True,
        "total": total,
        "by_status": by_status,
        "min_canonical_trust": MIN_CANONICAL_TRUST,
    }
