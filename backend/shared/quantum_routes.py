"""Quantum-Inspired State API.

Endpoints:
  POST /api/admin/quantum/preview    — compute a verdict from arbitrary inputs
  GET  /api/admin/quantum/doctrine    — return the doctrine + regime list
  GET  /api/admin/quantum/last        — last verdict produced by the council
                                        (populated when the council wires it in)
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from auth import get_current_user
from db import db
from namespaces import SHARED_GOVERNANCE_DECISIONS
from shared.quantum_state import (
    DOCTRINE_TEXT,
    REGIMES,
    BrainOpinion,
    build_quantum_inspired_state,
)


router = APIRouter(tags=["quantum"])


class OpinionIn(BaseModel):
    brain: str
    direction: str
    confidence: float = Field(ge=0.0, le=1.0)
    risk_bias: float = 1.0
    reason: str = ""


class QuantumPreviewIn(BaseModel):
    opinions: list[OpinionIn]
    market_features: Optional[dict] = None
    min_risk: float = 0.50
    max_risk: float = 1.25
    hold_lock_entropy_floor: float = 0.35


@router.get("/admin/quantum/doctrine")
async def doctrine(_user: dict = Depends(get_current_user)):  # noqa: B008
    """Doctrine surface — what this layer is allowed and not allowed to do."""
    return {
        "doctrine": DOCTRINE_TEXT,
        "regimes": list(REGIMES),
        "guarantees": {
            "may_change_direction": False,
            "may_promote_hold_to_trade": False,
            "may_modulate_risk_within_bounds": True,
            "may_signal_hold_lock": True,
        },
    }


@router.post("/admin/quantum/preview")
async def preview(body: QuantumPreviewIn, _user: dict = Depends(get_current_user)):  # noqa: B008
    """Compute a verdict from arbitrary inputs. Useful for testing
    overlay configurations against scenarios before wiring them into
    the council."""
    opinions = [BrainOpinion(**o.model_dump()) for o in body.opinions]
    verdict = build_quantum_inspired_state(
        opinions=opinions,
        market_features=body.market_features or {},
        min_risk=body.min_risk,
        max_risk=body.max_risk,
        hold_lock_entropy_floor=body.hold_lock_entropy_floor,
    )
    return {
        "verdict": verdict.to_dict(),
        "doctrine": DOCTRINE_TEXT,
    }


@router.get("/admin/quantum/recent")
async def recent_verdicts(
    limit: int = 20,
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    """Latest N governance rows with their quantum verdicts attached.
    Lets the operator see regime probabilities + HOLD-lock signals
    evolving over time. Empty quantum_state rows are filtered out so
    the panel shows only intents where the overlay actually ran."""
    limit = max(1, min(200, int(limit)))
    cursor = (
        db[SHARED_GOVERNANCE_DECISIONS]
        .find({"quantum_state": {"$exists": True}}, {"_id": 0})
        .sort("ts", -1)
        .limit(limit)
    )
    rows = await cursor.to_list(length=limit)
    out = []
    for r in rows:
        qs = r.get("quantum_state") or {}
        out.append({
            "ts": r.get("ts"),
            "intent_id": r.get("intent_id"),
            "symbol": r.get("symbol"),
            "lane": r.get("lane"),
            "executor": r.get("executor_seat_holder"),
            "governor": r.get("governor_seat_holder"),
            "opponent": r.get("opponent_seat_holder"),
            "action": r.get("executor_action"),
            "verdict_code": r.get("verdict_code"),
            "final_allowed": r.get("final_allowed"),
            "risk_multiplier": r.get("risk_multiplier"),
            "quantum": {
                "risk_multiplier": qs.get("risk_multiplier"),
                "entropy": qs.get("entropy"),
                "hold_lock_detected": qs.get("hold_lock_detected"),
                "regime_probs": qs.get("regime_probs"),
                "notes": qs.get("notes") or [],
                "pre_quantum_size": qs.get("pre_quantum_size"),
                "post_quantum_size": qs.get("post_quantum_size"),
            },
        })
    # Aggregate health-check counters for the panel header.
    counters = {
        "total_returned": len(out),
        "hold_locks": sum(1 for r in out if r["quantum"]["hold_lock_detected"]),
        "with_notes": sum(1 for r in out if r["quantum"]["notes"]),
    }
    return {"items": out, "counters": counters}
