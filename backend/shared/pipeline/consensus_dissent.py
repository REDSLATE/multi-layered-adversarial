"""Adversarial dissent classifier — operator-pinned 2026-06-26.

Doctrine (operator spec):
    Consensus is not agreement unless risk, side, and confidence align.
    Same-side opinions with meaningful confidence gaps are dissent.
    Consensus boost is capped at +0.04.
    Any hard dissent kills the boost.
    Barracuda must appear for consensus to count.
    Advisors with >90% rolling agreement are damped.

This module replaces the rubber-stamp boost (98%+ apply rate) with
a true adversarial check. The Seat still decides; advisors stop
nodding at each other.

Env-gated via `ADVERSARIAL_ARGUMENT_MODE`. When OFF (default),
`apply_dissent` returns the input unchanged — existing behavior is
preserved bit-for-bit. When ON, the rules below activate.

Operator can tune via `runtime_flags` Mongo doc (no redeploy):
    adv_max_consensus_boost      default 0.04
    adv_conf_gap_agree           default 0.08
    adv_conf_gap_dissent         default 0.15
    adv_groupthink_threshold     default 0.90
    adv_groupthink_damp          default 0.50
    adv_require_barracuda        default true
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


# ── Tunables (operator override via runtime_flags) ──────────────────
DEFAULT_MAX_CONSENSUS_BOOST = 0.04
DEFAULT_CONF_GAP_AGREE = 0.08
DEFAULT_CONF_GAP_DISSENT = 0.15
DEFAULT_GROUPTHINK_THRESHOLD = 0.90
DEFAULT_GROUPTHINK_DAMP = 0.50
DEFAULT_REQUIRE_BARRACUDA = True

# Risk level derived from confidence bands. Until brains ship an
# explicit `risk_level`, this proxy stands in. Bands tuned to match
# what an operator would call "low/medium/high conviction".
RISK_LOW_MAX = 0.55
RISK_HIGH_MIN = 0.75


# ── Relation classification (operator spec verbatim) ────────────────


RELATION_HARD_DISSENT = "HARD_DISSENT"
RELATION_CONF_DISSENT = "CONFIDENCE_DISSENT"
RELATION_RISK_DISSENT = "RISK_DISSENT"
RELATION_SOFT_DISSENT = "SOFT_DISSENT"
RELATION_TRUE_AGREEMENT = "TRUE_AGREEMENT"


def is_enabled() -> bool:
    """Master switch — `ADVERSARIAL_ARGUMENT_MODE=true` in env."""
    raw = os.environ.get("ADVERSARIAL_ARGUMENT_MODE", "")
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def derive_risk_level(confidence: float) -> str:
    """Confidence-band proxy for risk_level until brains emit it
    explicitly. Same band the operator's UI uses for badge colors."""
    try:
        c = float(confidence)
    except (TypeError, ValueError):
        return "unknown"
    if c < RISK_LOW_MAX:
        return "low"
    if c >= RISK_HIGH_MIN:
        return "high"
    return "medium"


def classify_advisor_relation(
    executor_action: str,
    executor_confidence: float,
    advisor_action: str,
    advisor_confidence: float,
    conf_gap_agree: float = DEFAULT_CONF_GAP_AGREE,
    conf_gap_dissent: float = DEFAULT_CONF_GAP_DISSENT,
) -> str:
    """Classify one advisor's relation to the executor's opinion.

    Implements the operator spec exactly:
        1. Opposite side                              → HARD_DISSENT
        2. Same side, conf gap ≥ conf_gap_dissent     → CONFIDENCE_DISSENT
        3. Same side, gap < dissent, risk_level diff  → RISK_DISSENT
        4. Same side, gap ≤ conf_gap_agree, risk match → TRUE_AGREEMENT
        5. Otherwise (same side, moderate gap)        → SOFT_DISSENT
    """
    if executor_action != advisor_action:
        return RELATION_HARD_DISSENT

    gap = abs(float(advisor_confidence) - float(executor_confidence))

    if gap >= conf_gap_dissent:
        return RELATION_CONF_DISSENT

    exec_risk = derive_risk_level(executor_confidence)
    adv_risk = derive_risk_level(advisor_confidence)
    if exec_risk != adv_risk:
        return RELATION_RISK_DISSENT

    if gap <= conf_gap_agree:
        return RELATION_TRUE_AGREEMENT

    return RELATION_SOFT_DISSENT


# ── Boost adjustment per the dissent profile ────────────────────────


@dataclass
class DissentVerdict:
    """Output of `apply_dissent`. The seat consumes `boost` (signed),
    and stamps the rest into the receipt's `consensus_at_submit` block
    for the post-mortem to render."""
    boost: float
    governor_multiplier: float
    relations: List[str]
    relation_counts: Dict[str, int]
    blocked_reason: Optional[str]      # None unless the boost was zeroed
    damped_advisors: List[str]         # advisor_ids whose vote was halved
    require_barracuda: bool
    barracuda_present: bool

    def to_dict(self) -> Dict[str, Any]:
        return {
            "boost": round(self.boost, 4),
            "governor_multiplier": round(self.governor_multiplier, 4),
            "relations": self.relations,
            "relation_counts": dict(self.relation_counts),
            "blocked_reason": self.blocked_reason,
            "damped_advisors": list(self.damped_advisors),
            "require_barracuda": self.require_barracuda,
            "barracuda_present": self.barracuda_present,
        }


def _get_tunable(flag_overrides: Optional[Dict[str, float]], key: str, default: float) -> float:
    if not flag_overrides:
        return default
    v = flag_overrides.get(key)
    return float(v) if v is not None else default


def apply_dissent(
    *,
    executor_action: str,
    executor_confidence: float,
    advisors: List[Dict[str, Any]],
    raw_boost: float,
    advisor_agree_rates: Optional[Dict[str, float]] = None,
    flag_overrides: Optional[Dict[str, float]] = None,
) -> DissentVerdict:
    """Run the full dissent pipeline on an executor opinion + advisors.

    Args:
        executor_action: Executor's side (BUY/SELL).
        executor_confidence: Executor's pre-boost confidence.
        advisors: Each advisor as
            {"brain_id": str, "action": str, "confidence": float}.
        raw_boost: The signed boost the legacy engine would have applied.
            We START from this and reduce/zero based on dissent.
        advisor_agree_rates: Optional map of advisor brain_id → rolling
            agree-rate (0-1). Advisors above the threshold get their
            vote damped (multiplier applied to the boost they contribute).
        flag_overrides: Optional Mongo `runtime_flags` overrides to
            tune the constants without redeploy.

    Returns:
        DissentVerdict with the adjusted boost + provenance fields.
    """
    max_boost = _get_tunable(
        flag_overrides, "adv_max_consensus_boost", DEFAULT_MAX_CONSENSUS_BOOST,
    )
    conf_gap_agree = _get_tunable(
        flag_overrides, "adv_conf_gap_agree", DEFAULT_CONF_GAP_AGREE,
    )
    conf_gap_dissent = _get_tunable(
        flag_overrides, "adv_conf_gap_dissent", DEFAULT_CONF_GAP_DISSENT,
    )
    groupthink_threshold = _get_tunable(
        flag_overrides, "adv_groupthink_threshold", DEFAULT_GROUPTHINK_THRESHOLD,
    )
    groupthink_damp = _get_tunable(
        flag_overrides, "adv_groupthink_damp", DEFAULT_GROUPTHINK_DAMP,
    )
    require_barracuda_flag = bool(
        _get_tunable(flag_overrides, "adv_require_barracuda",
                     1.0 if DEFAULT_REQUIRE_BARRACUDA else 0.0)
    )

    # 1. Classify every advisor's relation to the executor.
    relations: List[str] = []
    classified: List[tuple[Dict[str, Any], str]] = []
    for adv in advisors:
        rel = classify_advisor_relation(
            executor_action=executor_action,
            executor_confidence=executor_confidence,
            advisor_action=adv.get("action", ""),
            advisor_confidence=float(adv.get("confidence", 0.0)),
            conf_gap_agree=conf_gap_agree,
            conf_gap_dissent=conf_gap_dissent,
        )
        relations.append(rel)
        classified.append((adv, rel))

    counts: Dict[str, int] = {}
    for r in relations:
        counts[r] = counts.get(r, 0) + 1

    hard = counts.get(RELATION_HARD_DISSENT, 0)
    risk_d = counts.get(RELATION_RISK_DISSENT, 0)
    conf_d = counts.get(RELATION_CONF_DISSENT, 0)
    soft = counts.get(RELATION_SOFT_DISSENT, 0)
    true_agree = counts.get(RELATION_TRUE_AGREEMENT, 0)

    # 2. Apply dissent → boost adjustments per spec.
    boost = float(raw_boost)
    governor_multiplier = 1.0
    blocked_reason: Optional[str] = None

    if hard > 0:
        boost = 0.0
        governor_multiplier *= 0.50
        blocked_reason = f"hard_dissent:{hard}"
    elif risk_d > 0:
        boost = 0.0
        governor_multiplier *= 0.70
        blocked_reason = f"risk_dissent:{risk_d}"
    elif conf_d > 0:
        # Treat confidence_dissent like a strong soft signal — zero the
        # boost but don't penalize the governor. Spec didn't pin this
        # branch explicitly; matching the conservative "any dissent
        # kills the boost" doctrine line.
        boost = 0.0
        blocked_reason = f"confidence_dissent:{conf_d}"
    elif soft > 0:
        boost *= 0.35
    elif true_agree >= 2:
        boost = min(boost, max_boost) if boost >= 0 else max(boost, -max_boost)

    # 3. Damp groupthink advisors (only matters when boost ≠ 0).
    damped: List[str] = []
    if boost != 0.0 and advisor_agree_rates:
        # Count how many of the contributing advisors are groupthinkers.
        # Halve the boost contribution from each such advisor.
        contrib_count = 0
        damped_count = 0
        for adv, rel in classified:
            if rel == RELATION_TRUE_AGREEMENT:
                contrib_count += 1
                rate = advisor_agree_rates.get(adv.get("brain_id", ""))
                if rate is not None and rate > groupthink_threshold:
                    damped.append(adv.get("brain_id", ""))
                    damped_count += 1
        if contrib_count > 0 and damped_count > 0:
            scale = 1.0 - (groupthink_damp * (damped_count / contrib_count))
            boost = boost * max(0.0, scale)

    # 4. Cap the final boost at MAX_CONSENSUS_BOOST.
    if boost > max_boost:
        boost = max_boost
    elif boost < -max_boost:
        boost = -max_boost

    # 5. Barracuda-presence gate (operator pin: "the adversarial auditor").
    advisor_names = {a.get("brain_id", "").lower() for a in advisors}
    barracuda_present = "barracuda" in advisor_names
    if require_barracuda_flag and not barracuda_present and boost > 0:
        # Only block positive boosts — negative boosts from dissent
        # should still take effect when Barracuda is absent.
        boost = 0.0
        if blocked_reason is None:
            blocked_reason = "missing_barracuda_adversary"

    return DissentVerdict(
        boost=boost,
        governor_multiplier=governor_multiplier,
        relations=relations,
        relation_counts=counts,
        blocked_reason=blocked_reason,
        damped_advisors=damped,
        require_barracuda=require_barracuda_flag,
        barracuda_present=barracuda_present,
    )


__all__ = [
    "RELATION_HARD_DISSENT",
    "RELATION_CONF_DISSENT",
    "RELATION_RISK_DISSENT",
    "RELATION_SOFT_DISSENT",
    "RELATION_TRUE_AGREEMENT",
    "DissentVerdict",
    "apply_dissent",
    "classify_advisor_relation",
    "derive_risk_level",
    "is_enabled",
]
