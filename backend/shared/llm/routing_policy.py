"""
Routing policy — role × task × availability → (provider, model).

Doctrine pin (2026-02-XX, revision):
    External AIs are TEACHERS and FALLBACKS.
    RISE_AI is the OPERATING MIND.
    Local / self_trained models are the eventual PRIMARY brain.

    The router walks `PROVIDER_PRIORITY` and picks the FIRST provider
    that is both (a) ready (adapter `is_ready()` returns True) AND
    (b) promoted to at least ADVISOR or PRIMARY for the requested role.

    Role-pinned overrides (`ROLE_OVERRIDES`) are honored when their
    provider is ready and promoted. This preserves the "claude-sonnet
    for governor / gpt-5.1 for strategist" current defaults until
    local/self_trained earns its way up the priority list.

    Promotion lifecycle (per provider, per role):
      SHADOW   — adapter is ready, but answers are LOGGED ONLY
                 (router does not return them). Used to gather a
                 distillation corpus.
      ADVISOR  — answers usable as a fallback / second opinion.
      PRIMARY  — answers preferred. Higher-priority PRIMARY wins.
      OFFLINE  — never used.

    The promotion state lives in Mongo (`llm_provider_state`) and
    is mutated by the operator after `eval_harness` shows the
    candidate is reliable. Defaults pinned below.

Tripwires:
    * KNOWN_PROVIDERS, PROVIDER_PRIORITY, and PROMOTION_STATES
      are locked. Adding a new provider is intentional and
      requires updating the tripwire test.
    * `choose_model(...)` always returns a known provider with a
      non-empty model id.
"""
from __future__ import annotations

from typing import Dict, Optional, Set


# ─────────────────────── locked invariants ────────────────────────────


KNOWN_PROVIDERS = ("local", "self_trained", "openai", "anthropic", "gemini")


# Walked left-to-right. Earlier = more preferred. Locking local +
# self_trained at the top encodes the "leave-the-platform"
# doctrine: as soon as either is ready and promoted, it wins.
PROVIDER_PRIORITY = (
    "local",          # operator-hosted general-purpose LLM
    "self_trained",   # RISE_AI's own trained model
    "anthropic",      # teacher / fallback
    "openai",         # teacher / fallback
    "gemini",         # teacher / fallback
)


PROMOTION_STATES = ("SHADOW", "ADVISOR", "PRIMARY", "OFFLINE")


# Pinned model ids — match the Emergent universal-key catalog.
DEFAULT_OPENAI = "gpt-5.1"
DEFAULT_ANTHROPIC = "claude-sonnet-4-5-20250929"
DEFAULT_GEMINI = "gemini-2.5-pro"
DEFAULT_LOCAL = "qwen3-coder"
DEFAULT_SELF_TRAINED = "rise-ai-v0"


# Provider → default model.
DEFAULT_MODELS: Dict[str, str] = {
    "local": DEFAULT_LOCAL,
    "self_trained": DEFAULT_SELF_TRAINED,
    "openai": DEFAULT_OPENAI,
    "anthropic": DEFAULT_ANTHROPIC,
    "gemini": DEFAULT_GEMINI,
}


# Default promotion state per provider.
# - External providers ship at PRIMARY (they're what's running today).
# - Local + self_trained ship at SHADOW (logged, not consulted).
# Operator promotes via `llm_provider_state` Mongo doc; see
# `shared.llm.training.eval_harness`.
DEFAULT_PROMOTION_STATE: Dict[str, str] = {
    "local":        "SHADOW",
    "self_trained": "SHADOW",
    "openai":       "PRIMARY",
    "anthropic":    "PRIMARY",
    "gemini":       "PRIMARY",
}


# Role-pinned PREFERRED external provider. Used when no higher-
# priority candidate is both ready AND promoted. This preserves the
# current "claude for governor, gpt for strategist, gemini for
# opponent" assignments while local/self_trained earn their way up.
ROLE_OVERRIDES: Dict[str, Dict[str, str]] = {
    "strategist":   {"provider": "openai",    "model": DEFAULT_OPENAI},
    "governor":     {"provider": "anthropic", "model": DEFAULT_ANTHROPIC},
    "opponent":     {"provider": "gemini",    "model": DEFAULT_GEMINI},
    "memory":       {"provider": "anthropic", "model": DEFAULT_ANTHROPIC},
    "auditor":      {"provider": "anthropic", "model": DEFAULT_ANTHROPIC},
    "executor":     {"provider": "anthropic", "model": DEFAULT_ANTHROPIC},  # advisory only
    "local_shadow": {"provider": "local",     "model": DEFAULT_LOCAL},
}


# ─────────────────────── routing ──────────────────────────────────────


def choose_model(
    *,
    role: str,
    task: str,
    ready: Optional[Set[str]] = None,
    promotion: Optional[Dict[str, str]] = None,
) -> Dict[str, str]:
    """Pick the (provider, model) for a brain call.

    Args:
        role:    e.g. "strategist", "governor"
        task:    advisory free-form, logged into the ledger
        ready:   set of providers whose adapter `is_ready()` returned
                 True. None means "trust the defaults" (test path).
        promotion: per-provider promotion state from `llm_provider_state`
                 (or `DEFAULT_PROMOTION_STATE` if None).

    Selection order:
        1. Walk `PROVIDER_PRIORITY`. The FIRST provider that is
           BOTH ready AND >= ADVISOR for this role wins.
        2. If no priority candidate wins, fall back to the
           role-pinned `ROLE_OVERRIDES[role]` (its readiness is NOT
           re-checked — it's the safety net).
        3. If the role is unknown, return anthropic claude-sonnet.
    """
    ready_set = set(KNOWN_PROVIDERS) if ready is None else set(ready)
    promo = dict(DEFAULT_PROMOTION_STATE) if promotion is None else _merge_promotion(promotion)

    # 1. Priority walk
    for p in PROVIDER_PRIORITY:
        if p not in ready_set:
            continue
        state = promo.get(p, "SHADOW")
        if state in ("ADVISOR", "PRIMARY"):
            return {"provider": p, "model": DEFAULT_MODELS[p]}

    # 2. Role override (safety net — not gated by readiness, so a
    # totally cold pod can still answer with a known-good default).
    override = ROLE_OVERRIDES.get(role)
    if override:
        return dict(override)

    # 3. Unknown role → anthropic.
    return {"provider": "anthropic", "model": DEFAULT_ANTHROPIC}


def _merge_promotion(operator_state: Dict[str, str]) -> Dict[str, str]:
    """Layer operator-set state over the defaults."""
    merged = dict(DEFAULT_PROMOTION_STATE)
    for k, v in operator_state.items():
        if k in DEFAULT_MODELS and v in PROMOTION_STATES:
            merged[k] = v
    return merged
