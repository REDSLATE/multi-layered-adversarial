"""Self-trained adapter — RISE_AI's own model, served behind an HTTP
endpoint when the operator deploys their fine-tuned weights.

Doctrine pin:
    This adapter is the *intended endgame*. The router lists
    `self_trained` second in `PROVIDER_PRIORITY` — once the operator
    promotes it past SHADOW, it takes over from commercial APIs.

    Today it is a stub. The signature matches every other adapter
    so promotion is a one-flag change in `llm_provider_state`, not
    a code refactor.
"""
from __future__ import annotations

import os
from typing import Optional, Tuple


SELF_TRAINED_URL_ENV = "RISE_AI_SELF_TRAINED_URL"
SELF_TRAINED_TOKEN_ENV = "RISE_AI_SELF_TRAINED_TOKEN"


def is_ready() -> bool:
    """True iff the operator has deployed self-trained inference
    and given the kernel a URL + token to reach it."""
    return bool(os.environ.get(SELF_TRAINED_URL_ENV))


async def call_self_trained(
    *,
    model: str,
    prompt: str,
    system: str,
    session_id: str,
) -> Tuple[str, Optional[dict]]:
    url = os.environ.get(SELF_TRAINED_URL_ENV, "")
    if not url:
        return (
            "[self_trained:NOT_DEPLOYED] "
            f"model={model} session={session_id} — "
            f"set {SELF_TRAINED_URL_ENV} (and optionally "
            f"{SELF_TRAINED_TOKEN_ENV}) to point at your trained "
            "weights server. Replace this stub with an httpx call.",
            None,
        )
    return (
        f"[self_trained:STUB_HIT_URL] url={url} model={model} "
        f"session={session_id}",
        None,
    )
