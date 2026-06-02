"""Shelly Bus contracts — brain → MC memory proposals.

The brain pods (Alpha / Camaro / Chevelle / RedEye) submit memory
proposals over HTTP to MC. MC verifies, scores trust, and decides
whether the proposal becomes canonical memory (Mongo write) or stays
in the proposal pen for later review.

Authority pin:
    Brains DO NOT self-certify truth. Every brain → MC packet is
    tagged `authority: MEMORY_PROPOSAL_ONLY`. MC rewrites the
    authority field at every boundary so a tampered packet cannot
    smuggle in a different value.

Trust scoring (initial, operator-tunable):
    + verified_outcomes match           → 0.90  (VERIFIED)
    + ≥ MIN_CONVERGENCE brains agree    → 0.80  (CONVERGED)
    + otherwise                         → 0.35  (UNVERIFIED, stored
                                                  in proposal pen)

A proposal is promoted to canonical MCShelly shared memory only when
`trust_score >= MIN_CANONICAL_TRUST` (default 0.75). Below that, it's
parked in `shelly_memory_proposals` for operator review or for the
auto-converge scan in `shelly/verified_facts.py` to pick up later.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


PROPOSAL_AUTHORITY = "MEMORY_PROPOSAL_ONLY"
REVIEW_AUTHORITY = "MC_SHELLY_REVIEW_ONLY"
CANONICAL_AUTHORITY = "MEMORY_REASONING_ONLY"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class ShellyMemoryProposal:
    """Wire shape for brain → MC memory proposals. Frozen so a single
    instance can't be mutated while in transit."""
    source_brain: str
    lane: str                        # "equity" | "crypto"
    symbol: str
    event_type: str                  # "market_pattern", "execution_outcome", etc.
    text: str                        # human-readable memory body
    confidence: float = 0.0
    outcome: Optional[str] = None    # "pending" | "win" | "loss" | "flat" | ...
    regime: Optional[str] = None
    source_id: Optional[str] = None  # links back to a brain decision_id / intent_id
    metadata: dict[str, Any] = field(default_factory=dict)
    authority: str = PROPOSAL_AUTHORITY

    def to_doc(self) -> dict[str, Any]:
        """Wire-serializable. The authority tag is re-stamped at the
        boundary by MC so a tampered packet can't smuggle in a
        different value — but we still emit MEMORY_PROPOSAL_ONLY here
        so the proposal travels with its honest tag."""
        d = asdict(self)
        d["authority"] = PROPOSAL_AUTHORITY
        return d
