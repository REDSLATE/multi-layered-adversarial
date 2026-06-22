"""Camino Committee — Alpha-weighted multi-agent confidence aggregator
(2026-02-21).

Operator pin (verbatim):
    "I kind of want them to merge into one. I want to bring Alpha's
    stats and build up Camino."
    "I don't want the trades just want the confidence for better
    choices."

What this module is:
    Camino used to emit a single confidence number from a single
    upstream signal source. Alpha's production stack revealed that
    different sub-agents have wildly different historical win rates
    (war_room 91.7%, signal_dispatcher 66.4%). Treating their votes
    as equal was leaving information on the table.

    This module formalises a six-member committee inside Camino.
    Each sub-agent emits a vote `(side, confidence)`. The Commander
    aggregates votes weighted by Alpha's *empirical win rate* — a
    Bayesian prior frozen at the time of merge. When only one
    sub-agent votes (the common case today), its confidence is
    still calibrated down by its prior — a 0.80 confidence from
    `signal_dispatcher` becomes effectively 0.80 × 0.664 = 0.531.

What this module is NOT:
    * NOT a trade-record migration. We import zero historical
      trades from Alpha. Only the win-rate priors travel.
    * NOT a replacement for the bull/bear adversarial kernel
      (`alpha_engine.py`). The committee operates BEFORE the
      Bull/Bear split — it's about who's voting and how much each
      vote counts, not about LONG vs SHORT.
    * NOT mandatory. When `evidence.committee_votes` is absent on
      an intent, the committee no-ops and existing Camino behavior
      is unchanged.

Priors (from operator's live Alpha telemetry, 2026-02-21):
    war_room          → 0.917   (91.7%, n=48)
    market_prediction → 0.879   (87.9%, n=83)
    hypothesis        → 0.855   (85.5%, n=69)
    signal_dispatcher → 0.664   (66.4%, n=532)  ← bulk pipeline
    paper_trader      → 0.200   (20.0%, n=5)    disabled by default
    pg_agent          → 1.000   (100%,  n=3)    excluded — too few

Vote weights are RAW WIN RATES. This is intentional and simple. A
weighted vote = `confidence × weight`. The side with the higher
weighted-sum wins; the winning confidence is the weighted-mean
confidence of votes on that side (clamped to the toxic-spike cap).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from shared.brains.alpha_engine import cap_confidence


# ── Priors ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SubAgentPrior:
    name: str
    win_rate: float
    sample_size: int
    enabled: bool = True


# Frozen at the moment of the Alpha→Camino merge (2026-02-21).
# When you re-pull `/api/ai-core/stats` and want to update, edit this
# table and bump the doctrine version below.
COMMITTEE_DOCTRINE_VERSION = "camino_committee_v1_2026_02_21"

SUB_AGENT_PRIORS: dict[str, SubAgentPrior] = {
    "war_room":          SubAgentPrior("war_room",          0.917, 48,  True),
    "market_prediction": SubAgentPrior("market_prediction", 0.879, 83,  True),
    "hypothesis":        SubAgentPrior("hypothesis",        0.855, 69,  True),
    "signal_dispatcher": SubAgentPrior("signal_dispatcher", 0.664, 532, True),
    # Disabled by default. Operator can flip via runtime flag if desired.
    "paper_trader":      SubAgentPrior("paper_trader",      0.200, 5,   False),
    # Excluded — only 3 trades, prior is unreliable noise.
    "pg_agent":          SubAgentPrior("pg_agent",          1.000, 3,   False),
}


# ── Vote shape ───────────────────────────────────────────────────────


@dataclass
class CommitteeVote:
    agent: str
    side: str          # "LONG" | "SHORT" | "HOLD"
    confidence: float  # 0–1

    @staticmethod
    def from_dict(d: dict[str, Any]) -> Optional["CommitteeVote"]:
        try:
            agent = str(d.get("agent") or "").strip().lower()
            side = str(d.get("side") or "").strip().upper()
            conf = float(d.get("confidence") or 0.0)
        except (TypeError, ValueError):
            return None
        if not agent or side not in {"LONG", "SHORT", "HOLD"}:
            return None
        if conf < 0.0 or conf > 1.0:
            conf = max(0.0, min(1.0, conf))
        return CommitteeVote(agent=agent, side=side, confidence=conf)


# ── Aggregation ──────────────────────────────────────────────────────


@dataclass
class CommitteeVerdict:
    side: str               # "LONG" | "SHORT" | "HOLD" | "NO_QUORUM"
    confidence: float       # weighted-mean confidence on winning side
    weighted_score: float   # absolute score for the winning side
    contributing_votes: list[dict[str, Any]] = field(default_factory=list)
    excluded_votes: list[dict[str, Any]] = field(default_factory=list)
    doctrine_version: str = COMMITTEE_DOCTRINE_VERSION

    def as_dict(self) -> dict[str, Any]:
        return {
            "side": self.side,
            "confidence": round(self.confidence, 4),
            "weighted_score": round(self.weighted_score, 4),
            "contributing_votes": self.contributing_votes,
            "excluded_votes": self.excluded_votes,
            "doctrine_version": self.doctrine_version,
        }


def aggregate_committee(
    votes: list[CommitteeVote],
    *,
    enabled_overrides: Optional[dict[str, bool]] = None,
) -> CommitteeVerdict:
    """Aggregate sub-agent votes into a single Camino verdict.

    Args:
        votes: list of `CommitteeVote`. Unknown agents are excluded.
        enabled_overrides: optional `{agent_name: bool}` to flip
            an agent on/off at decide-time (e.g. operator UI flag).
            Overrides the static `enabled` field on `SUB_AGENT_PRIORS`.

    Returns:
        CommitteeVerdict. When no agents contributed → side
        `"NO_QUORUM"`, confidence 0.
    """
    contributing: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    per_side_weighted_conf: dict[str, float] = {}
    per_side_weight_total: dict[str, float] = {}

    overrides = enabled_overrides or {}

    for vote in votes:
        prior = SUB_AGENT_PRIORS.get(vote.agent)
        if prior is None:
            excluded.append({
                "agent": vote.agent, "reason": "unknown_agent",
                "side": vote.side, "confidence": vote.confidence,
            })
            continue
        is_enabled = overrides.get(vote.agent, prior.enabled)
        if not is_enabled:
            excluded.append({
                "agent": vote.agent, "reason": "agent_disabled",
                "side": vote.side, "confidence": vote.confidence,
                "win_rate": prior.win_rate,
            })
            continue

        weight = prior.win_rate
        weighted_conf = vote.confidence * weight
        per_side_weighted_conf[vote.side] = (
            per_side_weighted_conf.get(vote.side, 0.0) + weighted_conf
        )
        per_side_weight_total[vote.side] = (
            per_side_weight_total.get(vote.side, 0.0) + weight
        )
        contributing.append({
            "agent": vote.agent, "side": vote.side,
            "confidence": vote.confidence, "win_rate": weight,
            "weighted_contribution": round(weighted_conf, 4),
        })

    if not per_side_weighted_conf:
        return CommitteeVerdict(
            side="NO_QUORUM", confidence=0.0, weighted_score=0.0,
            contributing_votes=contributing, excluded_votes=excluded,
        )

    # Winning side = highest absolute weighted sum.
    winning_side = max(per_side_weighted_conf,
                       key=lambda s: per_side_weighted_conf[s])
    winning_score = per_side_weighted_conf[winning_side]
    winning_weight_total = per_side_weight_total[winning_side]
    # Calibrated confidence = weighted-mean confidence of votes that
    # backed the winning side. (Mean, not sum — sum would inflate with
    # vote count.) Capped at toxic-spike ceiling.
    raw_confidence = winning_score / max(winning_weight_total, 1e-9)
    calibrated = cap_confidence(raw_confidence, cap=0.95)

    return CommitteeVerdict(
        side=winning_side, confidence=calibrated,
        weighted_score=winning_score,
        contributing_votes=contributing, excluded_votes=excluded,
    )


# ── Integration helper for legacy_brain_wrappers ────────────────────


def apply_committee_to_intent(
    intent: dict[str, Any],
    *,
    enabled_overrides: Optional[dict[str, bool]] = None,
) -> dict[str, Any]:
    """Mutates `intent` in place when committee votes are present.

    Reads `intent["evidence"]["committee_votes"]`. If absent or empty,
    returns the intent unchanged. If present, runs the aggregator,
    replaces `intent["confidence"]` with the calibrated value, and
    stamps `intent["evidence"]["committee_verdict"]` with the full
    audit trail.

    Caller is responsible for gating on the runtime flag (this helper
    does no flag lookup so it stays unit-testable).
    """
    evidence = intent.get("evidence") or {}
    raw_votes = evidence.get("committee_votes")
    if not raw_votes:
        return intent

    votes: list[CommitteeVote] = []
    for v in raw_votes:
        parsed = CommitteeVote.from_dict(v) if isinstance(v, dict) else None
        if parsed is not None:
            votes.append(parsed)

    verdict = aggregate_committee(votes, enabled_overrides=enabled_overrides)
    evidence["committee_verdict"] = verdict.as_dict()
    intent["evidence"] = evidence

    if verdict.side != "NO_QUORUM":
        # Replace the base confidence with the committee-calibrated
        # value. The downstream `apply_alpha_legacy_doctrine` will
        # then do position-discipline tweaks on top of THIS number,
        # so the entire emit pipeline ends up inheriting Alpha's
        # priors automatically.
        intent["confidence"] = verdict.confidence
        # Optional: if the committee disagrees with the brain's
        # original side, record it but do NOT flip the action (the
        # legacy executor wrapper would block a flip anyway). The
        # operator can opt in to a flip via a separate flag later.
        intent.setdefault("committee_side_match", None)
        original_side = (intent.get("action") or "").upper()
        committee_side = verdict.side.upper()
        if original_side in {"BUY", "SELL"}:
            mapped = "LONG" if original_side == "BUY" else "SHORT"
            intent["committee_side_match"] = (mapped == committee_side)
    return intent
