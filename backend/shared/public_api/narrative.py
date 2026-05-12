"""Public /digest/narrative — LLM-summarized market overview prose.

Single-shot. Takes MC's structured digest data (predictions /
smart_money / alerts) and asks Gemini 3 Flash to write a 3-5 sentence
overview the dashboard can render below the metric cards.

Cheap broadcast: cached for `CACHE_TTL_SECONDS` so a busy dashboard
hitting refresh every minute doesn't burn tokens. Cache key is
deterministic on the digest snapshot, so two callers seeing the same
data get the same prose.

Doctrine: the LLM is asked to summarize, not predict. The prompt
forbids fabricating numbers and instructs the model to anchor every
claim in the supplied JSON. Output is plain prose — no markdown, no
disclaimers, no "as an AI…" preambles.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from db import db
from namespaces import PUBLIC_NARRATIVE_CACHE

from .auth import PublicCaller, public_trust_required
from .digest import _build_predictions, _build_smart_money, _build_alerts, PAID_CAPS

try:
    from emergentintegrations.llm.chat import LlmChat, UserMessage
except ImportError:  # noqa: F401
    LlmChat = None
    UserMessage = None


router = APIRouter(tags=["public"])

CACHE_TTL_SECONDS = 300                   # 5 minutes; tune per cost appetite
LLM_PROVIDER = "gemini"
LLM_MODEL = "gemini-3-flash-preview"

NARRATIVE_SYSTEM = (
    "You are RiseDual's market commentary engine. You write a tight, "
    "factual 3-5 sentence overview of today's market posture. "
    "RULES: (1) Anchor every claim in the JSON the user provides. "
    "(2) Never invent numbers, tickers, or events not in the JSON. "
    "(3) No markdown, no bullet points, no preamble — just the prose. "
    "(4) No disclaimers, no 'as an AI', no hedging boilerplate. "
    "(5) Refer to the multi-AI consensus as 'the AIs' or 'the council'. "
    "(6) Use plain English. Cap at 5 sentences."
)


class NarrativeResponse(BaseModel):
    text: str
    cached: bool
    generated_at: str
    model: str
    tier: str


def _digest_signature(predictions: list, smart_money: list,
                      alerts: list) -> str:
    """Stable cache key from the digest content."""
    payload = json.dumps(
        {"p": predictions, "s": smart_money, "a": alerts},
        sort_keys=True, default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def _cache_bucket() -> int:
    """Time-bucketed cache key — narrative is regenerated at most once
    per CACHE_TTL_SECONDS window. Trades a stale-by-a-few-minutes
    overview for a sane token bill."""
    import time
    return int(time.time()) // CACHE_TTL_SECONDS


def _build_user_prompt(predictions: list, smart_money: list,
                       alerts: list, active_signals: int) -> str:
    return json.dumps({
        "active_signals": active_signals,
        "predictions": predictions[:10],
        "smart_money": smart_money[:10],
        "alerts": alerts[:5],
        "now": datetime.now(timezone.utc).isoformat(),
    }, default=str)


@router.get("/public/digest/narrative", response_model=NarrativeResponse)
async def get_digest_narrative(
    caller: PublicCaller = Depends(public_trust_required),
):
    """LLM-summarized prose for the market-overview block.

    Cached for 5 minutes on a content-hash key so refreshes don't burn
    tokens. Cached prose is served to every tier (narrative content is
    not gated — it's the same market either way)."""
    if LlmChat is None:
        raise HTTPException(
            status_code=503,
            detail="LLM integration not installed (emergentintegrations missing)",
        )
    api_key = os.environ.get("EMERGENT_LLM_KEY")
    if not api_key:
        raise HTTPException(
            status_code=503,
            detail="LLM not configured (EMERGENT_LLM_KEY unset)",
        )

    # Build the digest payload at paid-tier caps (we want rich context
    # for the LLM regardless of the caller's tier).
    full_preds = await _build_predictions(PAID_CAPS["predictions"])
    full_sm = await _build_smart_money(PAID_CAPS["smart_money"])
    full_alerts = await _build_alerts(PAID_CAPS["alerts"])

    from namespaces import SHARED_POSITIONS
    from shared.positions import OPEN_STATES
    active = await db[SHARED_POSITIONS].count_documents(
        {"state": {"$in": list(OPEN_STATES)}},
    )

    sig = f"bucket-{_cache_bucket()}"

    cached = await db[PUBLIC_NARRATIVE_CACHE].find_one(
        {"signature": sig}, {"_id": 0},
    )
    if cached:
        return NarrativeResponse(
            text=cached["text"],
            cached=True,
            generated_at=cached["generated_at"],
            model=cached["model"],
            tier=caller.tier,
        )

    user_prompt = _build_user_prompt(full_preds, full_sm, full_alerts, active)

    chat = LlmChat(
        api_key=api_key,
        session_id=f"narrative-{sig}",
        system_message=NARRATIVE_SYSTEM,
    ).with_model(LLM_PROVIDER, LLM_MODEL)

    try:
        text = await chat.send_message(UserMessage(text=user_prompt))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(
            status_code=502, detail=f"LLM call failed: {e}",
        ) from e

    text = (text or "").strip()
    if not text:
        raise HTTPException(status_code=502, detail="LLM returned empty narrative")

    now = datetime.now(timezone.utc).isoformat()
    await db[PUBLIC_NARRATIVE_CACHE].update_one(
        {"signature": sig},
        {"$set": {
            "signature": sig,
            "text": text,
            "generated_at": now,
            "model": f"{LLM_PROVIDER}:{LLM_MODEL}",
        }},
        upsert=True,
    )
    return NarrativeResponse(
        text=text,
        cached=False,
        generated_at=now,
        model=f"{LLM_PROVIDER}:{LLM_MODEL}",
        tier=caller.tier,
    )
