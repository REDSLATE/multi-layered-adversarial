"""Market-evidence citation requirement for advisor opinions.

Operator doctrine (2026-06-26):
    A brain may not agree or disagree unless it cites market fields.
    Consensus without cited market evidence does not boost confidence.
    Dissent with cited market evidence overrides shallow agreement.

Without this layer, an advisor brain can rubber-stamp the executor
by saying "BUY @ 0.70" with no reference to the actual market — that
opinion looks identical to a brain that examined RSI, VWAP, spread,
and volume before agreeing. This module forces the brains to cite
the data they used, and downweights any opinion that doesn't.

Migration posture: SOFT.
    Brains that haven't been upgraded to cite evidence still emit
    opinions; their contribution is multiplied by
    `WEIGHT_NO_EVIDENCE` (0.25). Once a brain ships citations, its
    weight returns to 1.0. This lets us roll out per-brain without
    a flag day.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional


# ── Spec dataclasses ─────────────────────────────────────────────────


@dataclass(frozen=True)
class MarketSnapshot:
    """Canonical per-symbol market state every brain sees. Brains
    cite fields by name (e.g. `evidence_fields=["rsi","vwap"]`).
    The validator rejects citations that aren't in this schema."""
    symbol: str
    price: float
    vwap: float
    rsi: float
    atr_pct: float
    volume_rel: float
    spread_bps: float
    trend_5m: float
    trend_1h: float
    news_score: Optional[float] = None


@dataclass(frozen=True)
class AdvisorOpinion:
    """Single-brain opinion enriched with market-evidence citations.

    `evidence_fields` — names of MarketSnapshot keys the brain
        consulted before forming this opinion. Validator requires ≥ 3
        and rejects unknown names.
    `objection` — semicolon-joined codes (e.g. "RSI_OVERBOUGHT;SPREAD_TOO_WIDE"),
        None when the advisor agrees without reservation.
    """
    brain: str
    side: str
    confidence: float
    risk_level: str
    evidence_fields: List[str] = field(default_factory=list)
    objection: Optional[str] = None


# ── Validator ────────────────────────────────────────────────────────


MIN_CITED_FIELDS = 3
_ALLOWED_FIELDS = set(MarketSnapshot.__annotations__.keys())


def validate_opinion(opinion: AdvisorOpinion) -> None:
    """Raise ValueError if the opinion doesn't meet the evidence-
    citation contract. Called at the boundary where a brain hands
    an opinion to the consensus engine."""
    if len(opinion.evidence_fields) < MIN_CITED_FIELDS:
        raise ValueError(
            "advisor_opinion_missing_market_evidence: "
            f"got {len(opinion.evidence_fields)} fields, "
            f"need ≥ {MIN_CITED_FIELDS}"
        )
    for f in opinion.evidence_fields:
        if f not in _ALLOWED_FIELDS:
            raise ValueError(
                f"invalid_market_field: {f!r} not in MarketSnapshot schema"
            )


def is_evidence_backed(opinion_dict: Dict) -> bool:
    """Cheap check for an opinion already serialized into the
    consensus pool. Used by `apply_dissent` to decide weight."""
    fields = opinion_dict.get("evidence_fields") or []
    if not isinstance(fields, list) or len(fields) < MIN_CITED_FIELDS:
        return False
    return all(f in _ALLOWED_FIELDS for f in fields)


def has_objection(opinion_dict: Dict) -> bool:
    """An opinion with a non-empty `objection` field is doing the
    adversarial work — cite-driven dissent. These get full weight
    even when they agree with the executor's side."""
    obj = opinion_dict.get("objection")
    return isinstance(obj, str) and len(obj.strip()) > 0


# ── Doctrine weights ─────────────────────────────────────────────────


# Opinion without evidence citation OR objection: only 25% of normal
# weight. The doctrine line: "Consensus without cited market evidence
# does not boost confidence."
WEIGHT_NO_EVIDENCE = 0.25
WEIGHT_FULL = 1.0


def advisor_weight(opinion_dict: Dict) -> float:
    """Return the multiplier applied to this advisor's vote when the
    dissent layer aggregates boost contributions.

    Rules (operator doctrine):
        * Evidence-backed (≥3 valid cited fields) → full weight
        * Has an objection (semicolon-joined codes) → full weight
          (the brain did adversarial work — count it even if it
          ultimately agreed)
        * Neither → WEIGHT_NO_EVIDENCE (0.25) — rubber-stamper
    """
    if is_evidence_backed(opinion_dict) or has_objection(opinion_dict):
        return WEIGHT_FULL
    return WEIGHT_NO_EVIDENCE


# ── Reference advisor (operator-provided sample) ─────────────────────


def barracuda_advisor(
    snapshot: MarketSnapshot, executor_side: str,
) -> AdvisorOpinion:
    """Reference implementation matching the operator's spec.

    Generates concrete, citation-backed objections from snapshot
    facts. Other brains can mirror this shape — each brain's
    objection set differs by its doctrine (Barracuda = mean-reversion
    so it flags overbought BUYs; a momentum brain would flag the
    opposite).
    """
    objections: List[str] = []

    if snapshot.rsi > 72 and executor_side == "BUY":
        objections.append("RSI_OVERBOUGHT_AGAINST_BUY")
    if snapshot.rsi < 28 and executor_side == "SELL":
        objections.append("RSI_OVERSOLD_AGAINST_SELL")
    if snapshot.price < snapshot.vwap and executor_side == "BUY":
        objections.append("PRICE_BELOW_VWAP_AGAINST_BUY")
    if snapshot.price > snapshot.vwap and executor_side == "SELL":
        objections.append("PRICE_ABOVE_VWAP_AGAINST_SELL")
    if snapshot.spread_bps > 75:
        objections.append("SPREAD_TOO_WIDE")
    if snapshot.volume_rel < 1.2:
        objections.append("WEAK_RELATIVE_VOLUME")

    return AdvisorOpinion(
        brain="BARRACUDA",
        side="HOLD" if objections else executor_side,
        confidence=0.62 if objections else 0.48,
        risk_level="HIGH" if objections else "NORMAL",
        evidence_fields=["rsi", "vwap", "spread_bps", "volume_rel"],
        objection=";".join(objections) or None,
    )


__all__ = [
    "MarketSnapshot",
    "AdvisorOpinion",
    "MIN_CITED_FIELDS",
    "WEIGHT_NO_EVIDENCE",
    "WEIGHT_FULL",
    "validate_opinion",
    "is_evidence_backed",
    "has_objection",
    "advisor_weight",
    "barracuda_advisor",
]
