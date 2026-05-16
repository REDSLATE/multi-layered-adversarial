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
