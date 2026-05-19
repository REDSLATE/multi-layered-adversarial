"""Governor policy — the FATAL/SILENCE taxonomy translated into a
reusable post-process that any council/routing path can call.

Doctrine pin (2026-05-18): only FATAL governor reasons may stop
execution. Silence / soft dissent / no-stance → risk-down (50% floor,
clamped at 10% so a 0.0 input doesn't zero out).

The taxonomy sets are imported from `shared.council` so there is ONE
source of truth (the council module also imports them at boot). Two
identical copies would drift.
"""
from __future__ import annotations

from typing import Any, Dict, Tuple

from shared.council import (
    FATAL_GOVERNOR_REASONS,
    SILENCE_GOVERNOR_REASONS,
)


# Soft-dissent-below-floor is also non-fatal but is not in the
# SILENCE set (which is about "Chevelle didn't say anything"). Keep
# it as a separate category so the policy can treat it the same way.
SILENCE_OR_SOFT_REASONS: frozenset[str] = SILENCE_GOVERNOR_REASONS | frozenset({
    "SOFT_DISSENT_BELOW_FLOOR",
})

# Floor on the risk multiplier after silence-downgrade. We never want
# to silently zero a trade — if it's downgraded, it executes at this
# minimum size or higher.
SILENCE_RISK_FLOOR: float = 0.10
SILENCE_RISK_HALVING: float = 0.50


def apply_governor_policy(
    governance: Dict[str, Any],
    *,
    executable: bool,
    size_mult: float,
) -> Tuple[bool, float, Dict[str, Any]]:
    """Apply the FATAL/SILENCE taxonomy to a governance verdict.

    Input shape (permissive):
        governance["status"]  — "BLOCK" / "ALLOW" / "WARN" / etc.
        governance["reason"]  — the specific reason code

    Returns:
        (executable, size_mult, governance) — a copy of governance
        with `execution_effect` and `display_status` set.

    Decision matrix:
        status != BLOCK                              → ALLOW (pass through)
        status == BLOCK + reason in FATAL            → HARD_BLOCK (kill)
        status == BLOCK + reason in SILENCE_OR_SOFT  → RISK_DOWN_ONLY (50%, floor 10%)
        status == BLOCK + reason unknown             → RISK_DOWN_ONLY (conservative)
    """
    status = str(governance.get("status") or "").upper()
    reason = str(governance.get("reason") or "").upper()

    governance = dict(governance)

    if status != "BLOCK":
        governance["execution_effect"] = "ALLOW"
        governance["display_status"] = status or "ALLOW"
        return executable, size_mult, governance

    if reason in FATAL_GOVERNOR_REASONS:
        governance["execution_effect"] = "HARD_BLOCK"
        governance["display_status"] = "BLOCK"
        return False, 0.0, governance

    if reason in SILENCE_OR_SOFT_REASONS:
        governance["execution_effect"] = "RISK_DOWN_ONLY"
        governance["display_status"] = "RISK_DOWN"
        return executable, max(size_mult * SILENCE_RISK_HALVING, SILENCE_RISK_FLOOR), governance

    # Unknown BLOCK reason → treat as soft (conservative default; do not
    # kill the trade on an unrecognized reason string). Operator can
    # explicitly add it to FATAL_GOVERNOR_REASONS if it should kill.
    governance["execution_effect"] = "RISK_DOWN_ONLY"
    governance["display_status"] = "RISK_DOWN"
    return executable, max(size_mult * SILENCE_RISK_HALVING, SILENCE_RISK_FLOOR), governance
