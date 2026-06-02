"""RISE AI role profiles — keyed by SEAT (not brain).

Doctrine pin (2026-02-17 refactor):
    Authority lives on the SEAT, not the brain holding it. So the
    training and promotion ladder also lives on the seat. An auditor
    model trained today on RedEye's calls keeps its identity even if
    Alpha takes the auditor seat tomorrow — the seat is the unit of
    promotion in the 8-seat IP and in `ai_checkpoints`.

The 8 canonical seats (4 equity + 4 crypto):
    Equity:
        strategist  — opportunity / conviction reasoning
        auditor     — adversarial downside + post-trade review
        governor    — risk / drawdown / policy
        executor    — execution / fills / slippage
    Crypto (lane-isolated twin):
        crypto_strategist
        crypto_auditor
        crypto_governor
        crypto      (= crypto_executor)

Each profile drives:
    * the system-prompt scaffold for that seat's LLM calls
      (focus + forbidden lists become the role-aligned safety frame)
    * the checkpoint `model_id` used by the AI autonomy pipeline
    * the seat-flavored memory category labelling on the Shelly layer

Authority: PURE DATA. Zero I/O. Nothing here executes, routes, blocks,
or promotes. Seat policy and RoadGuard remain the only authority layer.
"""
from __future__ import annotations

from typing import Any, Dict


# ─── Equity lane ───────────────────────────────────────────────────────
_STRATEGIST = {
    "model_id": "rise-ai-strategist-qwen3-8b-v1",
    "purpose": "market opportunity reasoning",
    "focus": ["trend", "breakout", "compression", "volume expansion", "regime transition"],
    "forbidden": ["chase weak signal", "ignore downside", "invent confidence"],
}

_AUDITOR = {
    "model_id": "rise-ai-auditor-qwen3-8b-v1",
    "purpose": "adversarial downside + post-trade review",
    "focus": ["trap", "fake breakout", "collapse", "reversal", "bear case",
              "post-trade attribution", "rule violations"],
    "forbidden": ["ignore bull case", "overfit fear", "invent collapse"],
}

_GOVERNOR = {
    "model_id": "rise-ai-governor-qwen3-8b-v1",
    "purpose": "governance and risk reasoning",
    "focus": ["drawdown", "wide spread", "volatility", "liquidity danger", "policy violations"],
    "forbidden": ["approve unsafe action", "remove guard", "override RoadGuard"],
}

_EXECUTOR = {
    "model_id": "rise-ai-executor-qwen3-8b-v1",
    "purpose": "execution reasoning",
    "focus": ["fills", "slippage", "spread", "broker readiness", "position sizing"],
    "forbidden": ["ignore risk", "force execution", "bypass broker gate"],
}


# ─── Crypto lane (twin profiles, crypto-flavored focus) ────────────────
_CRYPTO_STRATEGIST = {
    "model_id": "rise-ai-crypto-strategist-qwen3-8b-v1",
    "purpose": "crypto market opportunity reasoning",
    "focus": ["funding rate", "perp basis", "exchange flow", "dominance shift",
              "breakout", "compression", "regime transition"],
    "forbidden": ["chase parabolic", "ignore wick risk", "invent confidence"],
}

_CRYPTO_AUDITOR = {
    "model_id": "rise-ai-crypto-auditor-qwen3-8b-v1",
    "purpose": "crypto adversarial downside + post-trade review",
    "focus": ["liquidation cascade", "wick hunt", "fake breakout",
              "stablecoin depeg", "exchange risk", "bear case",
              "post-trade attribution"],
    "forbidden": ["ignore bull case", "overfit fear", "invent collapse"],
}

_CRYPTO_GOVERNOR = {
    "model_id": "rise-ai-crypto-governor-qwen3-8b-v1",
    "purpose": "crypto governance and risk reasoning",
    "focus": ["funding spike", "drawdown", "exchange withdrawal halt",
              "stablecoin risk", "leverage stack", "policy violations"],
    "forbidden": ["approve unsafe action", "remove guard", "override RoadGuard"],
}

_CRYPTO_EXECUTOR = {
    "model_id": "rise-ai-crypto-executor-qwen3-8b-v1",
    "purpose": "crypto execution reasoning",
    "focus": ["fills", "slippage", "spread", "pair liquidity",
              "Kraken readiness", "position sizing"],
    "forbidden": ["ignore risk", "force execution", "bypass broker gate"],
}


RISE_AI_ROLE_PROFILES: Dict[str, Dict[str, Any]] = {
    # Equity lane
    "strategist": _STRATEGIST,
    "auditor": _AUDITOR,
    "governor": _GOVERNOR,
    "executor": _EXECUTOR,
    # Crypto lane (canonical 8-seat IP names)
    "crypto_strategist": _CRYPTO_STRATEGIST,
    "crypto_auditor": _CRYPTO_AUDITOR,
    "crypto_governor": _CRYPTO_GOVERNOR,
    "crypto": _CRYPTO_EXECUTOR,
}


# Profile used when an unknown role/seat is queried. Graceful fallback;
# never raises.
GENERAL_PROFILE: Dict[str, Any] = {
    "model_id": "rise-ai-general-qwen3-8b-v1",
    "purpose": "general reasoning",
    "focus": [],
    "forbidden": [],
}


# Legacy alias map (2026-02-17). Some MC-side callers still pass these
# names; resolve to the canonical seat before lookup so they get the
# right profile without crashing or returning GENERAL_PROFILE.
_LEGACY_ALIASES: Dict[str, str] = {
    "decider": "strategist",
    "opponent": "auditor",
    "advisor": "auditor",
    "crypto_decider": "crypto_strategist",
    "crypto_opponent": "crypto_auditor",
    "crypto_executor": "crypto",
}


def profile_for(role: str) -> Dict[str, Any]:
    """Return the profile for a seat, falling back to GENERAL_PROFILE.

    Resolves legacy seat names (`decider`, `opponent`, `crypto_decider`,
    `crypto_opponent`, `crypto_executor`, `advisor`) to their canonical
    equivalents before lookup so older callers don't silently get the
    general profile.
    """
    key = (role or "").lower()
    key = _LEGACY_ALIASES.get(key, key)
    return RISE_AI_ROLE_PROFILES.get(key, GENERAL_PROFILE)


def model_for_role(role: str) -> str:
    """Return the checkpoint `model_id` for a seat. Used by the AI
    autonomy pipeline (`register_checkpoint`, `set_checkpoint_state`).
    Falls back to `rise-ai-general-qwen3-8b-v1`.
    """
    return profile_for(role).get("model_id", GENERAL_PROFILE["model_id"])
