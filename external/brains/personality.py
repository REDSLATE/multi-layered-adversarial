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
"""
from __future__ import annotations


BRAIN_PERSONALITIES: dict[str, dict[str, object]] = {
    "camino": {
        "display_name": "Camino",
        "confidence_mult": 1.00,
        "risk_mode": "balanced",
    },
    "barracuda": {
        "display_name": "Barracuda",
        "confidence_mult": 1.15,
        "risk_mode": "opportunistic",
    },
    "hellcat": {
        "display_name": "Hellcat",
        "confidence_mult": 1.30,
        "risk_mode": "aggressive",
    },
    "gto": {
        "display_name": "GTO",
        "confidence_mult": 0.85,
        "risk_mode": "disciplined",
    },
}


# We pin the brain_id → personality mapping with the same keys MC
# uses internally. Camino runs as `alpha`, Barracuda as `camaro`,
# etc. (legacy holdover from the external-sidecar era — the
# operator-facing display name is the one in the table above).
_BRAIN_ID_TO_PERSONALITY = {
    "alpha": "camino",
    "camaro": "barracuda",
    "chevelle": "hellcat",
    "redeye": "gto",
    # Also accept the display names directly so callers don't have
    # to remember the mapping.
    "camino": "camino",
    "barracuda": "barracuda",
    "hellcat": "hellcat",
    "gto": "gto",
}


def get_personality(brain: str) -> dict[str, object]:
    """Resolve a brain identifier (brain_id OR display name) to its
    personality config. Unknown brains return the neutral default."""
    key = _BRAIN_ID_TO_PERSONALITY.get((brain or "").lower())
    return BRAIN_PERSONALITIES.get(key or "", {
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


__all__ = ["BRAIN_PERSONALITIES", "apply_personality", "get_personality"]
