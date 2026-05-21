"""OpenAI adapter — routes through the Emergent universal key while
on platform; trivially swappable to a direct `OPENAI_API_KEY` later.

Why emergentintegrations (today): the universal key is what the
operator is actively paying for. We use the same client surface as
the rest of the backend (`LlmChat`).
"""
from __future__ import annotations

import os
from typing import Optional, Tuple

from emergentintegrations.llm.chat import LlmChat, UserMessage


def _key() -> str:
    k = os.environ.get("EMERGENT_LLM_KEY") or os.environ.get("OPENAI_API_KEY")
    if not k:
        raise RuntimeError(
            "no LLM key available: set EMERGENT_LLM_KEY (on platform) "
            "or OPENAI_API_KEY (self-hosted)"
        )
    return k


def is_ready() -> bool:
    """True iff some key is configured (universal or direct)."""
    return bool(os.environ.get("EMERGENT_LLM_KEY") or os.environ.get("OPENAI_API_KEY"))


async def call_openai(
    *,
    model: str,
    prompt: str,
    system: str,
    session_id: str,
) -> Tuple[str, Optional[dict]]:
    chat = LlmChat(
        api_key=_key(),
        session_id=session_id,
        system_message=system,
    ).with_model("openai", model)
    text = await chat.send_message(UserMessage(text=prompt))
    return (text or ""), None
