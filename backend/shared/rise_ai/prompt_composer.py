"""Role-aligned RISE AI prompt composer — single source of truth.

This is the canonical implementation of the brain-side `_compose_prompt`
contract. Every brain pod (Alpha / Camaro / Chevelle / RedEye) imports
this same function so the safety frame, doctrine pin, and output schema
are identical across the fleet — no per-pod drift.

Authority pin (loud):
    The output schema includes `authority: REASONING_ONLY`. Brain code
    that asks an LLM for an action_hint must NEVER act on `BUY/SELL/HOLD`
    without going through MC's intent endpoint. The hint is REASONING,
    not authorization.

Output contract (every brain returns this shape):
    action_hint: BUY/SELL/HOLD/ANALYZE
    confidence: 0.00-1.00
    reason:
    risks:
    memory_used:
    role_alignment:
    authority: REASONING_ONLY
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from shared.rise_ai.role_profiles import profile_for


def compose_role_aligned_prompt(
    *,
    role: str,
    prompt: str,
    memory_context: str = "",
    market_context: Optional[Dict[str, Any]] = None,
    doctrine_context: Optional[Dict[str, Any]] = None,
) -> str:
    """Compose the role-aligned prompt scaffold for a RISE AI brain call.

    Args:
        role: brain name — `alpha`, `camaro`, `chevelle`, `redeye`,
              or anything else (falls back to general profile).
        prompt: the actual user/operator prompt being reasoned over.
        memory_context: serialized verified memory snippet to inject.
        market_context: structured market state (price, vol, regime, ...).
        doctrine_context: structured doctrine state (seat holder,
              lane toggle, exposure caps, ...).

    Returns:
        The fully composed prompt string. No I/O, no side effects.
    """
    profile = profile_for(role)
    mc_str = str(market_context) if market_context else "{}"
    dc_str = str(doctrine_context) if doctrine_context else "{}"
    return f"""
RISE AI ROLE: {role}
MODEL PURPOSE: {profile.get("purpose", "general reasoning")}

ROLE FOCUS:
{profile.get("focus", [])}

FORBIDDEN BEHAVIOR:
{profile.get("forbidden", [])}

DOCTRINE:
- You reason only.
- You do not place trades.
- You do not call brokers.
- You do not bypass Paradox / MC.
- You do not bypass RoadGuard.
- Return structured output only.
- Authority is REASONING_ONLY.

VERIFIED MEMORY:
{memory_context or "No verified memory."}

MARKET CONTEXT:
{mc_str}

DOCTRINE CONTEXT:
{dc_str}

PROMPT:
{prompt}

Return exactly:
action_hint: BUY/SELL/HOLD/ANALYZE
confidence: 0.00-1.00
reason:
risks:
memory_used:
role_alignment:
authority: REASONING_ONLY
""".strip()
