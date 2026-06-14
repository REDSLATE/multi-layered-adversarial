"""DisagreementMetrics — governor reads vote-distribution to decide size cuts.

Given a list of BrainVotes (one per brain), produce a frozen metric
bundle the governor can act on:

  entropy             — normalised Shannon entropy over stance counts;
                        0.0 = unanimous, 1.0 = maximum split
  outlier_brain       — the dissenting brain (if any) with the highest
                        calibrated confidence
  outlier_stance      — what the outlier voted
  regime_mismatch     — hook for the verifier to flag regimes that
                        historically show high disagreement
  abstention_rate     — fraction of brains that returned ABSTAIN
  majority_stance     — most-common non-abstaining stance
  majority_confidence — mean calibrated confidence of the majority

Governor side-effects (computed downstream by the caller, NOT here —
this module is pure):
  * entropy > 0.7              → size multiplier 0.5
  * abstention_rate > 0.4      → force human review (vote_required)
  * outlier_brain present      → verifier flags brain for deep-dive

This module never imports the seat or roadguard layer. It only reads
BrainVote shapes. That's the IP boundary.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from math import log2
from typing import Optional

from shared.brain_vote import BrainVote


@dataclass(frozen=True)
class DisagreementMetrics:
    entropy: float
    outlier_brain: Optional[str]
    outlier_stance: Optional[str]
    regime_mismatch: bool
    abstention_rate: float
    majority_stance: Optional[str]
    majority_confidence: float


def compute_disagreement(
    votes: list[BrainVote],
    regime: str,  # noqa: ARG001 — reserved for regime_mismatch hydration
) -> DisagreementMetrics:
    if not votes:
        # No votes → treat as maximum disagreement and full abstention.
        return DisagreementMetrics(
            entropy=1.0,
            outlier_brain=None,
            outlier_stance=None,
            regime_mismatch=False,
            abstention_rate=1.0,
            majority_stance=None,
            majority_confidence=0.0,
        )

    active = [v for v in votes if v.stance != "ABSTAIN"]
    abstention_rate = (len(votes) - len(active)) / len(votes)

    if not active:
        # Everyone abstained — that's a clear signal in itself, not a
        # disagreement state. Entropy is undefined; we return 1.0 to
        # match the "max risk-down" branch downstream callers expect.
        return DisagreementMetrics(
            entropy=1.0,
            outlier_brain=None,
            outlier_stance=None,
            regime_mismatch=False,
            abstention_rate=round(abstention_rate, 4),
            majority_stance=None,
            majority_confidence=0.0,
        )

    stances = [v.stance for v in active]
    counts = Counter(stances)
    total = len(stances)

    # Normalised Shannon entropy.
    raw_entropy = -sum((c / total) * log2(c / total) for c in counts.values())
    if len(counts) > 1:
        max_entropy = log2(len(counts))
        entropy = raw_entropy / max_entropy
    else:
        entropy = 0.0

    majority_stance, majority_count = counts.most_common(1)[0]
    majority_votes = [v for v in active if v.stance == majority_stance]
    majority_confidence = (
        sum(v.calibrated_confidence for v in majority_votes) / len(majority_votes)
    )

    # Outlier: only meaningful if the majority is < 75% (i.e. the
    # minority is substantial enough to surface). The verifier-flagged
    # brain is the most-confident dissenter — that's the most
    # actionable signal for a deep-dive.
    outlier: Optional[str] = None
    outlier_stance: Optional[str] = None
    if len(counts) > 1 and (majority_count / total) < 0.75:
        minority = [v for v in active if v.stance != majority_stance]
        if minority:
            top = max(minority, key=lambda v: v.calibrated_confidence)
            outlier = top.brain
            outlier_stance = top.stance

    return DisagreementMetrics(
        entropy=round(entropy, 4),
        outlier_brain=outlier,
        outlier_stance=outlier_stance,
        regime_mismatch=False,  # verifier hydrates this in a later session
        abstention_rate=round(abstention_rate, 4),
        majority_stance=majority_stance,
        majority_confidence=round(majority_confidence, 4),
    )
