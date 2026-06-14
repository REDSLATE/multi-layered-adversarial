"""VerifierReplay — failure attribution for losing trades.

Given a ReplayCase (votes, governor output, roadguard decision, seat
action, actual P&L outcome), produce a FailureReason classifying:

  ACCEPTABLE_LOSS    — loss within tolerance OR no position taken
  GOVERNOR_ERROR     — no brain supported this direction; governor /
                       seat traded anyway
  BRAIN_ERROR        — at least one brain supported the losing
                       direction; flag the most-confident wrong brain
                       and detect:
                         * calibration_error      (loss>100bps + conf>0.7)
                         * memory_error            (memory said win_rate>0.6)
                         * negative_knowledge_miss (most peers abstained)
  ROADGUARD_MISS     — roadguard was OPEN when historical data shows
                       it should have been BLOCKED (TODO: needs the
                       historical-stop reconstruction module — flagged
                       here for spec parity, not wired)
  REGIME_SHIFT       — market regime changed post-entry (TODO: needs
                       intraday regime tracking — flagged here for
                       spec parity, not wired)

The verifier does NOT mutate brain state. It writes a FailureReason
record to a queue; the brain ingests it at maintenance time. That's
the temporal separation between doctrine (brain) and audit (verifier).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional

from shared.brain_vote import BrainVote


class FailureType(str, Enum):
    BRAIN_ERROR = "brain_error"
    GOVERNOR_ERROR = "governor_error"
    ROADGUARD_MISS = "roadguard_miss"
    ACCEPTABLE_LOSS = "acceptable_loss"
    REGIME_SHIFT = "regime_shift"


@dataclass(frozen=True)
class FailureReason:
    type: FailureType
    responsible_brain: Optional[str] = None
    calibration_error: bool = False
    memory_error: bool = False
    negative_knowledge_miss: bool = False
    explanation: str = ""


@dataclass
class ReplayCase:
    timestamp: datetime
    symbol: str
    regime: str
    brain_votes: dict[str, BrainVote]
    governor_output: dict[str, Any]
    roadguard_decision: str  # "OPEN" or "BLOCKED"
    seat_action: dict[str, Any]   # {direction: BUY|SELL|HOLD, notional_usd: …}
    actual_outcome: dict[str, Any] = field(default_factory=dict)  # {pnl_bps, exit_time, …}


class VerifierReplay:
    def __init__(self, loss_threshold_bps: int = -50) -> None:
        # Losses shallower (i.e. closer to 0) than this are absorbed
        # as ACCEPTABLE_LOSS without deeper attribution.
        self.loss_threshold_bps = loss_threshold_bps

    def analyze(self, case: ReplayCase) -> FailureReason:
        pnl = float(case.actual_outcome.get("pnl_bps") or 0.0)

        # Shallow losses don't trigger attribution.
        if pnl > self.loss_threshold_bps:
            return FailureReason(
                type=FailureType.ACCEPTABLE_LOSS,
                explanation=f"Loss {pnl} bps within threshold {self.loss_threshold_bps} bps",
            )

        direction = case.seat_action.get("direction", "HOLD")
        if direction == "HOLD":
            return FailureReason(
                type=FailureType.ACCEPTABLE_LOSS,
                explanation="No position taken — nothing to attribute",
            )

        # Who supported the losing direction?
        supporters = [
            (brain_id, vote)
            for brain_id, vote in case.brain_votes.items()
            if vote.stance == direction
        ]

        if not supporters:
            # No brain voted in this direction → governor/seat acted
            # against the consensus. That's a governor error.
            return FailureReason(
                type=FailureType.GOVERNOR_ERROR,
                explanation=f"No brain supported {direction}; governor/seat traded anyway",
            )

        # Most-confident wrong brain takes the attribution hit.
        worst_brain, worst_vote = max(
            supporters, key=lambda x: x[1].calibrated_confidence,
        )

        # Calibration error: brain was very confident but very wrong.
        calibration_error = (
            worst_vote.calibrated_confidence > 0.7 and pnl < -100.0
        )

        # Memory error: brain leaned on optimistic memory evidence.
        memory_error = False
        if worst_vote.memory_evidence is not None:
            mem = worst_vote.memory_evidence
            memory_error = mem.win_rate > 0.6 and pnl < -100.0

        # Negative-knowledge miss: more than half of peers abstained
        # but this brain didn't — its negative-knowledge layer should
        # have caught the same setup.
        negative_miss = False
        if pnl < -100.0:
            others = [
                v for b, v in case.brain_votes.items() if b != worst_brain
            ]
            if others:
                abstain_share = (
                    sum(1 for v in others if v.stance == "ABSTAIN") / len(others)
                )
                if abstain_share > 0.5:
                    negative_miss = True

        return FailureReason(
            type=FailureType.BRAIN_ERROR,
            responsible_brain=worst_brain,
            calibration_error=calibration_error,
            memory_error=memory_error,
            negative_knowledge_miss=negative_miss,
            explanation=(
                f"Brain {worst_brain} supported {direction} with "
                f"calibrated_confidence={worst_vote.calibrated_confidence}, "
                f"resulted in {pnl} bps"
            ),
        )
