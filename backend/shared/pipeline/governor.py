"""Governor — modifier-only. NEVER blocks.

Reads `opinion.evidence["risk_multiplier"]` (set upstream by the
doctrine layer or the brain itself) and clamps it to [0.05, 1.0].
That's the entire contract.

Doctrine-layer outputs that previously caused blocks (RISK_DOWN ×0.25,
DOCTRINE_REJECT, etc.) are translated into risk_multiplier values in
the upstream brain/doctrine glue — by the time they reach the
Governor, they are a number, not a verdict.
"""
from __future__ import annotations

from .models import BrainOpinion, GovernorModifier


class Governor:
    """Stateless. Pure function over opinion.evidence."""

    async def modify(self, opinion: BrainOpinion) -> GovernorModifier:
        raw = opinion.evidence.get("risk_multiplier", 1.0)
        try:
            mult = float(raw)
        except (TypeError, ValueError):
            mult = 1.0
        clamped = max(0.05, min(mult, 1.0))
        reason = (
            "governor_modifier_only"
            if clamped == mult
            else f"governor_modifier_clamped:{mult:.3f}->{clamped:.3f}"
        )
        return GovernorModifier(risk_multiplier=clamped, reason=reason)
