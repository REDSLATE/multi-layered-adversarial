"""Brain doctrine layer — bind interpretation to identity, not to seat.

Doctrine pin (operator directive, 2026-06-XX, post-AAPL incident):

    Do not hard-lock personality to seat.

        brain_id  = who it is        (Camino, Barracuda, Hellcat, GTO)
        doctrine  = how it thinks    (trend, mean_reversion, breakout, momentum)
        seat      = what job it is doing today
                    (strategist, executor, governor, auditor)

    Camino can be executor today, auditor tomorrow.
    But Camino still thinks like a trend-following brain.

    Result:
        Seat decides responsibility.
        Doctrine decides interpretation.
        Brain ID decides personality.

    That gives real disagreement without locking brains into fixed jobs.

The four brains were previously running the same `NeutralAdversarialBrain`
evaluator × 4 with different display names. On the AAPL 06-09 incident
all four "agreed" on BUY within 2 seconds of each other because there
was nothing to disagree about — they were the same algorithm shouted
four times. This module gives each brain a distinct interpretation
function so the adversarial layer can actually be adversarial.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


BrainID = Literal["camino", "barracuda", "hellcat", "gto"]
Seat = Literal["strategist", "executor", "governor", "auditor"]
DoctrineName = Literal["trend", "mean_reversion", "breakout", "momentum"]


@dataclass(frozen=True)
class BrainDoctrine:
    brain_id: BrainID
    display_name: str
    doctrine: DoctrineName

    lookback_short: int
    lookback_long: int

    min_confidence: float
    min_gap: float

    trend_weight: float
    mean_reversion_weight: float
    breakout_weight: float
    momentum_weight: float
    risk_weight: float

    aggression: float


DOCTRINES: dict[str, BrainDoctrine] = {
    "camino": BrainDoctrine(
        brain_id="camino",
        display_name="Camino",
        doctrine="trend",
        lookback_short=20,
        lookback_long=50,
        min_confidence=0.62,
        min_gap=0.08,
        trend_weight=1.40,
        mean_reversion_weight=0.60,
        breakout_weight=0.90,
        momentum_weight=0.90,
        risk_weight=1.00,
        aggression=0.90,
    ),
    "barracuda": BrainDoctrine(
        brain_id="barracuda",
        display_name="Barracuda",
        doctrine="mean_reversion",
        lookback_short=14,
        lookback_long=30,
        min_confidence=0.58,
        min_gap=0.06,
        trend_weight=0.70,
        mean_reversion_weight=1.50,
        breakout_weight=0.70,
        momentum_weight=0.80,
        risk_weight=1.10,
        aggression=1.00,
    ),
    "hellcat": BrainDoctrine(
        brain_id="hellcat",
        display_name="Hellcat",
        doctrine="breakout",
        lookback_short=10,
        lookback_long=20,
        min_confidence=0.64,
        min_gap=0.10,
        trend_weight=0.90,
        mean_reversion_weight=0.50,
        breakout_weight=1.60,
        momentum_weight=1.10,
        risk_weight=0.90,
        aggression=1.15,
    ),
    "gto": BrainDoctrine(
        brain_id="gto",
        display_name="GTO",
        doctrine="momentum",
        lookback_short=8,
        lookback_long=21,
        min_confidence=0.60,
        min_gap=0.07,
        trend_weight=1.00,
        mean_reversion_weight=0.50,
        breakout_weight=1.00,
        momentum_weight=1.60,
        risk_weight=0.95,
        aggression=1.10,
    ),
}


# ── Bridge: legacy MC `stack` slot codes → canonical brain_id ─────
#
# MC's wire protocol (intents, audit log, dashboard) carries a
# `stack` field with the legacy slot codes from when each brain was
# a separate sidecar process. The slot codes are still used by the
# routing layer and the dashboard, but `brain_id` is the canonical
# identity going forward. This map is the only place the mapping
# lives. Anything that needs to translate between them imports here.
STACK_TO_BRAIN_ID: dict[str, str] = {
    # Canonical → canonical (identity)
    "camino": "camino",
    "barracuda": "barracuda",
    "hellcat": "hellcat",
    "gto": "gto",
    # Legacy → new canonical (accepted at ingress for back-compat)
    "alpha": "camino",
    "camaro": "barracuda",
    "chevelle": "hellcat",
    "redeye": "gto",
}

BRAIN_ID_TO_STACK: dict[str, str] = {v: k for k, v in STACK_TO_BRAIN_ID.items()}


def get_doctrine(brain_id: str) -> BrainDoctrine:
    """Resolve a doctrine by canonical brain_id (camino/barracuda/
    hellcat/gto) OR by legacy stack code (alpha/camaro/chevelle/
    redeye). Operator code can use either — the lookup tolerates
    both during the rename transition.
    """
    key = (brain_id or "").lower().strip()
    if key in STACK_TO_BRAIN_ID:
        key = STACK_TO_BRAIN_ID[key]
    if key not in DOCTRINES:
        raise ValueError(f"Unknown brain_id: {brain_id!r}")
    return DOCTRINES[key]


__all__ = [
    "BrainID", "Seat", "DoctrineName", "BrainDoctrine",
    "DOCTRINES", "STACK_TO_BRAIN_ID", "BRAIN_ID_TO_STACK",
    "get_doctrine",
]
