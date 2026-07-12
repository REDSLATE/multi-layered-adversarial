"""Per-brain personality multipliers.

Doctrine: brain personalities are CONFIDENCE MODULATORS only. They
shape the rate at which a given brain's intents trip the learning
ladder's promotion threshold — opportunistic brains (Barracuda) get
to micro_live faster on a strong signal; disciplined brains (GTO)
require more evidence. They DO NOT modify action (BUY/SELL/HOLD)
and they DO NOT add any gate on top of MC.

Every restriction lives in MC's existing layer:
    broker_lane_toggles  (operator runtime kill switch per lane)
    learning ladder      (per-brain-per-lane stage gates)
    sizing_gate          (route="observe" enforcement)
    exposure_caps        (RISEDUAL_CAP_PER_ORDER_USD etc.)
    MC receipt           (broker adapters require it)

The personality multiplier just shapes how loud the brain's "this
is a strong read" signal is — not whether the trade happens.

Identity convention (2026-06-XX): internal DB / API ids are
alpha / camaro / chevelle / redeye (slot codes — never user-
facing). Display names are Camino / Barracuda / Hellcat / GTO
(the operator brand names rendered everywhere in the UI).
"""
from __future__ import annotations


BRAIN_PERSONALITIES: dict[str, dict[str, object]] = {
    "alpha": {
        "display_name": "Camino",
        "confidence_mult": 1.00,
        "risk_mode": "balanced",
    },
    "camaro": {
        "display_name": "Barracuda",
        "confidence_mult": 1.15,
        "risk_mode": "opportunistic",
    },
    "chevelle": {
        "display_name": "Hellcat",
        "confidence_mult": 1.30,
        "risk_mode": "aggressive",
    },
    "redeye": {
        "display_name": "GTO",
        "confidence_mult": 0.85,
        "risk_mode": "disciplined",
    },
}


def get_personality(brain: str) -> dict[str, object]:
    """Resolve a brain identifier to its personality config. Unknown
    brains return the neutral default (balanced, ×1.00)."""
    key = (brain or "").lower()
    return BRAIN_PERSONALITIES.get(key, {
        "display_name": brain,
        "confidence_mult": 1.00,
        "risk_mode": "balanced",
    })


def apply_personality(brain: str, confidence: float) -> float:
    """Multiply `confidence` by the brain's personality bias and clamp
    to the valid probability range [0.0, 1.0].

    Clamp is mathematical only — keeping `confidence` a valid
    probability. There is NO soft gate here. Every restriction
    lives in MC (lane toggles, exposure caps, sizing_gate, ladder),
    where the operator controls it at runtime.
    """
    persona = get_personality(brain)
    mult = float(persona.get("confidence_mult", 1.0))
    adjusted = float(confidence) * mult
    return max(0.0, min(1.0, adjusted))


def clamp_probability(value: float) -> float:
    """Math clamp — ensures `value` is a valid probability in
    [0.0, 1.0]. This is NOT a soft gate; it's a domain check.
    Anything outside [0,1] is nonsense for a confidence value.
    """
    return max(0.0, min(1.0, float(value)))


def apply_personality_confidence(
    brain: str,
    raw_confidence: float,
) -> tuple[float, dict]:
    """Apply the brain's personality multiplier AND return an audit
    trail of every touch on the value.

    Returns:
        (final_confidence, evidence_dict)

    The evidence dict is attached to the intent so the operator can
    inspect — for every intent in the audit log — exactly:
        * What raw conviction the brain core produced
        * What multiplier the brain's personality applied
        * What the final emitted value was
        * Which code paths touched the value

    No silent dampening: if the math clamp at 1.0 ever bites, that
    fact is recorded on the intent. The operator sees the truth.
    """
    persona = get_personality(brain)
    multiplier = float(persona.get("confidence_mult", 1.0))
    risk_mode = persona.get("risk_mode", "balanced")
    raw = float(raw_confidence)
    adjusted = raw * multiplier
    final = clamp_probability(adjusted)
    # Was the clamp the binding constraint? Surface it as a separate
    # boolean so audit views can flag honestly-saturated reads vs.
    # ordinary multiplied reads.
    saturated = adjusted != final
    evidence = {
        "raw_confidence": raw,
        "personality_multiplier": multiplier,
        "personality_risk_mode": risk_mode,
        "adjusted_pre_clamp": adjusted,
        "final_confidence": final,
        "saturated_by_clamp": saturated,
        "confidence_touched_by": ["personality.py", "math_clamp_0_1"],
    }
    return final, evidence


__all__ = [
    "BRAIN_PERSONALITIES", "apply_personality", "get_personality",
    "clamp_probability", "apply_personality_confidence",
]
