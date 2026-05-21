"""
Provider promotion state — operator-mutable, Mongo-persisted.

Doctrine pin:
    The operator promotes a candidate provider through SHADOW →
    ADVISOR → PRIMARY. The router reads this state to decide who
    actually serves a brain call. Eval_harness writes scores
    against the candidate over time; the operator inspects them
    and toggles promotion.

Collection: `llm_provider_state` — singleton doc keyed by provider.
"""
from __future__ import annotations

import logging
from typing import Dict

from db import db
from namespaces import LLM_PROVIDER_STATE
from shared.llm.routing_policy import (
    DEFAULT_PROMOTION_STATE, PROMOTION_STATES,
)

logger = logging.getLogger("risedual.llm_kernel.provider_state")


async def get_promotion_states() -> Dict[str, str]:
    """Read the operator-set state from Mongo, layered over defaults.

    Best-effort: a Mongo outage returns defaults so the kernel still
    routes (commercial PRIMARY) and the brain still gets an answer.
    """
    try:
        out = dict(DEFAULT_PROMOTION_STATE)
        cursor = db[LLM_PROVIDER_STATE].find({}, {"_id": 0})
        async for doc in cursor:
            provider = doc.get("provider")
            state = doc.get("state")
            if (provider in DEFAULT_PROMOTION_STATE
                    and state in PROMOTION_STATES):
                out[provider] = state
        return out
    except Exception as e:  # noqa: BLE001
        logger.warning("get_promotion_states failed, using defaults: %s", e)
        return dict(DEFAULT_PROMOTION_STATE)


async def set_promotion_state(provider: str, state: str, *, note: str = "") -> Dict[str, str]:
    """Operator-only mutation. Validated against the known sets."""
    if provider not in DEFAULT_PROMOTION_STATE:
        raise ValueError(f"unknown provider: {provider!r}")
    if state not in PROMOTION_STATES:
        raise ValueError(f"unknown promotion state: {state!r}")
    await db[LLM_PROVIDER_STATE].update_one(
        {"provider": provider},
        {
            "$set": {
                "provider": provider,
                "state": state,
                "note": note or None,
            },
        },
        upsert=True,
    )
    return await get_promotion_states()
