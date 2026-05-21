"""Anthropic adapter — routes through Emergent universal key."""
from __future__ import annotations

import os
from typing import Optional, Tuple

from emergentintegrations.llm.chat import LlmChat, UserMessage


def _key() -> str:
    k = os.environ.get("EMERGENT_LLM_KEY") or os.environ.get("ANTHROPIC_API_KEY")
    if not k:
        raise RuntimeError(
            "no LLM key available: set EMERGENT_LLM_KEY (on platform) "
            "or ANTHROPIC_API_KEY (self-hosted)"
        )
    return k


def is_ready() -> bool:
    return bool(os.environ.get("EMERGENT_LLM_KEY") or os.environ.get("ANTHROPIC_API_KEY"))


async def call_anthropic(
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
    ).with_model("anthropic", model)
    text = await chat.send_message(UserMessage(text=prompt))
    return (text or ""), None
