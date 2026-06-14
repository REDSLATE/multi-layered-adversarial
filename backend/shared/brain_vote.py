"""BrainVote — immutable per-brain decision record (Paradox v2).

Doctrine (locked 2026-02-19):
    Seat always decides. Autonomy mode decides whether that decision
    becomes an order. Observe and shadow modes do not simulate trades.
    Live orders begin only at toehold mode.

BrainVote is the IMMUTABLE record a brain hands the governor. It carries:
  - the brain's stance (BUY|SELL|HOLD|ABSTAIN),
  - both raw and calibrated confidence (so the verifier can grade
    calibration quality, not just stance),
  - the calibration key the brain used (so the verifier can call out
    underspecified or gamed buckets — e.g. claiming a 0.9 confidence
    bucket with only 3 historical samples),
  - the memory evidence (similar-pattern win-rate) the brain leaned on,
  - whether negative knowledge was triggered (forces ABSTAIN),
  - a frozen tuple of reasoning strings,
  - a timestamp.

The dataclass is `frozen=True`. Construction enforces invariants in
__post_init__; once a vote exists, it cannot be mutated. This is the
audit-trail contract — the verifier always sees what the brain
actually emitted, not a post-hoc edit.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, Optional


Stance = Literal["BUY", "SELL", "HOLD", "ABSTAIN"]


# Maximum allowed |calibrated − raw| in a single vote. The earlier
# one-sided rule (calibrated ≤ raw + 0.05) was wrong: a brain that is
# historically underconfident in a regime should be allowed to
# calibrate UP (e.g. raw 0.62 → calibrated 0.68 if observed win rate
# is 0.74). A symmetric delta of 0.30 lets honest calibration in both
# directions while still flunking gaming (raw 0.10, calibrated 0.95).
CALIBRATION_DELTA_MAX: float = 0.30


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class CalibrationKey:
    """Bucket key the brain used to look up its historical performance.

    The verifier audits the (regime, conf_bucket) pair against the
    brain's actual calibration history. If the brain claims a key
    that has fewer than `min_samples` historical observations, the
    verifier downgrades the trust on this vote.
    """
    regime: str
    conf_bucket: float


@dataclass(frozen=True)
class MarketMemoryResult:
    """Similar-pattern lookup result the brain attached as evidence.

    `failure_pattern` carries an optional negative-pattern hash if the
    brain's memory layer flagged this setup as historically toxic.
    """
    similar_count: int
    win_rate: float
    avg_return_bps: float
    worst_drawdown_bps: float
    failure_pattern: Optional[str] = None


@dataclass(frozen=True)
class BrainVote:
    brain: str
    stance: Stance
    calibrated_confidence: float
    raw_confidence: float
    calibration_key: CalibrationKey
    memory_evidence: Optional[MarketMemoryResult]
    negative_knowledge_triggered: bool
    reasoning: tuple[str, ...]  # frozen — never list
    timestamp: datetime

    def __post_init__(self) -> None:
        # Confidence bounds
        if not 0.0 <= self.calibrated_confidence <= 1.0:
            raise ValueError(
                f"calibrated_confidence must be in [0, 1], got {self.calibrated_confidence}"
            )
        if not 0.0 <= self.raw_confidence <= 1.0:
            raise ValueError(
                f"raw_confidence must be in [0, 1], got {self.raw_confidence}"
            )

        # Abstain contract — the privileged state requires justification.
        if self.stance == "ABSTAIN" and not self.negative_knowledge_triggered:
            raise ValueError("ABSTAIN requires negative_knowledge_triggered=True")
        if self.stance == "ABSTAIN" and self.calibrated_confidence != 0.0:
            raise ValueError("ABSTAIN must have calibrated_confidence=0.0")

        # Symmetric calibration-delta cap (Paradox v2 doctrine).
        # ABSTAIN votes bypass this — their calibrated is forced to 0.0
        # which can be arbitrarily far from raw_confidence by design.
        if self.stance != "ABSTAIN":
            delta = abs(self.calibrated_confidence - self.raw_confidence)
            if delta > CALIBRATION_DELTA_MAX:
                raise ValueError(
                    f"calibration delta too large: |calibrated − raw| = {delta:.4f} "
                    f"> {CALIBRATION_DELTA_MAX} "
                    f"(raw={self.raw_confidence}, calibrated={self.calibrated_confidence})"
                )

        # Reasoning must be a non-empty frozen tuple.
        if not isinstance(self.reasoning, tuple):
            raise ValueError("reasoning must be a tuple (frozen, no mutation)")
        if len(self.reasoning) == 0:
            raise ValueError("reasoning must contain at least one entry")

    @classmethod
    def abstain(
        cls,
        brain: str,
        reason: str,
        calibration_key: CalibrationKey,
        raw_confidence: float,
        timestamp: Optional[datetime] = None,
    ) -> "BrainVote":
        """Factory for abstention votes — enforces the negative_knowledge contract.

        Brains MUST NOT hand-construct ABSTAIN votes. Calling this
        factory guarantees the abstain invariants hold.
        """
        return cls(
            brain=brain,
            stance="ABSTAIN",
            calibrated_confidence=0.0,
            raw_confidence=raw_confidence,
            calibration_key=calibration_key,
            memory_evidence=None,
            negative_knowledge_triggered=True,
            reasoning=(reason,),
            timestamp=timestamp or _now_utc(),
        )
