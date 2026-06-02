"""RISE AI role profiles — pure data registry, no I/O.

One row per brain. The profile drives:
    * the system-prompt scaffold for that brain's LLM calls
      (focus + forbidden lists become the role-aligned safety frame)
    * the checkpoint `model_id` used by the AI autonomy pipeline
    * the brain-flavored memory category labelling on the Shelly layer

Authority pin:
    These profiles are READ-ONLY DATA. They do not execute, route,
    block, or promote. Changing a profile changes what a brain MODELS,
    not what it can DO — seat policy + RoadGuard remain the only
    authority layer.

Profiles ship at qwen3-8b base. All four brains start from the same
base model and DIVERGE through role-specific fine-tuning, evaluated
through the `ai_autonomy` pipeline (`build_training_jsonl` →
`shadow_compare` → `evaluate_candidate_model` → operator state change).
"""
from __future__ import annotations

from typing import Any, Dict


RISE_AI_ROLE_PROFILES: Dict[str, Dict[str, Any]] = {
    "alpha": {
        "model_id": "rise-ai-alpha-qwen3-8b-v1",
        "purpose": "execution reasoning",
        "focus": ["fills", "slippage", "spread", "broker readiness", "position sizing"],
        "forbidden": ["ignore risk", "force execution", "bypass broker gate"],
    },
    "camaro": {
        "model_id": "rise-ai-camaro-qwen3-8b-v1",
        "purpose": "market opportunity reasoning",
        "focus": ["trend", "breakout", "compression", "volume expansion", "regime transition"],
        "forbidden": ["chase weak signal", "ignore downside", "invent confidence"],
    },
    "chevelle": {
        "model_id": "rise-ai-chevelle-qwen3-8b-v1",
        "purpose": "governance and risk reasoning",
        "focus": ["drawdown", "wide spread", "volatility", "liquidity danger", "policy violations"],
        "forbidden": ["approve unsafe action", "remove guard", "override RoadGuard"],
    },
    "redeye": {
        "model_id": "rise-ai-redeye-qwen3-8b-v1",
        "purpose": "adversarial downside reasoning",
        "focus": ["trap", "fake breakout", "collapse", "reversal", "bear case"],
        "forbidden": ["ignore bull case", "overfit fear", "invent collapse"],
    },
}


GENERAL_PROFILE: Dict[str, Any] = {
    "model_id": "rise-ai-general-qwen3-8b-v1",
    "purpose": "general reasoning",
    "focus": [],
    "forbidden": [],
}


def profile_for(role: str) -> Dict[str, Any]:
    """Return the profile for a role, falling back to GENERAL_PROFILE.

    Never raises on unknown roles — RISE AI gracefully degrades to a
    generic reasoning frame so a typo in a new brain name doesn't
    crash a live brain call.
    """
    return RISE_AI_ROLE_PROFILES.get((role or "").lower(), GENERAL_PROFILE)


def model_for_role(role: str) -> str:
    """Return the checkpoint `model_id` for a brain role.

    Used by the AI autonomy pipeline (`register_checkpoint`,
    `set_checkpoint_state`) and any external trainer that wants to
    pull the canonical id. Falls back to `rise-ai-general-qwen3-8b-v1`
    when the role is unknown.
    """
    return profile_for(role).get("model_id", GENERAL_PROFILE["model_id"])
