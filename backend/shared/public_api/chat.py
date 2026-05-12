"""Public /chat — grounded RiseDualGPT for Pro Max.

Multi-turn chat using Claude Sonnet 4.5 via the Emergent LLM key.
Grounded in MC's open positions + recent indicator snapshots — the
brain doesn't make up market data, it speaks about real positions and
real indicators.

Doctrine:
  * Pro Max only. Free / starter / pro all get 403. risedual.ai
    handles credit deduction BEFORE calling MC; MC simply refuses
    callers below pro_max.
  * Session memory is persisted to MongoDB (`public_chat_messages`)
    keyed by `session_id`. Multi-turn survives MC restarts because we
    replay history into a fresh LlmChat each request.
  * Hard cap of 50 turns per session. Beyond that the oldest turns
    drop off (rolling window) — keeps token cost bounded.
  * Refuses to talk about anything outside trading, markets,
    technical analysis, or RiseDual's own outputs (system prompt
    enforces).
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from db import db
from namespaces import (
    PUBLIC_CHAT_MESSAGES,
    SHARED_INDICATOR_SNAPSHOTS,
    SHARED_POSITIONS,
    SHARED_POSITION_STANCES,
)
from shared.positions import OPEN_STATES

from .auth import PublicCaller, public_trust_required

try:
    from emergentintegrations.llm.chat import LlmChat, UserMessage
except ImportError:  # noqa: F401
    LlmChat = None
    UserMessage = None


router = APIRouter(tags=["public"])

LLM_PROVIDER = "anthropic"
LLM_MODEL = "claude-sonnet-4-5-20250929"

# Max retained turns per session. One "turn" = one user + one assistant
# message. We replay these on every call, so this directly bounds token
# cost per chat call.
MAX_TURNS_PER_SESSION = 25

SYSTEM_PROMPT = (
    "You are RiseDualGPT — RiseDual's grounded trading-analysis assistant. "
    "You answer questions about: markets, technical analysis, specific tickers, "
    "RiseDual's own AI signals, multi-brain consensus, and trading concepts. "
    "\n"
    "GROUNDING: Every response anchors on the SYSTEM CONTEXT provided below "
    "(today's open signals, recent technicals, AI consensus). If the user "
    "asks about a symbol or signal that ISN'T in the context, say so plainly. "
    "Do NOT make up prices, indicators, news, or signals not in the context.\n"
    "\n"
    "SCOPE: If the user asks about anything unrelated to trading/markets "
    "(politics, personal life, code, weather), politely redirect them.\n"
    "\n"
    "STYLE: Tight, plain English. Cite the specific signal_id or symbol when "
    "you reference something from the context. No 'as an AI', no hedging "
    "boilerplate, no markdown headings, no disclaimers. Default to 2-4 "
    "paragraphs unless the question warrants more depth.\n"
    "\n"
    "DOCTRINE: RiseDual is observation-only — never advise the user to "
    "execute a trade. You may explain what the AIs are signaling and why; "
    "you may NOT recommend buying or selling. If asked 'should I buy?', "
    "answer with what the signals say, not with personal advice."
)


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000)
    session_id: Optional[str] = Field(default=None, max_length=64)


class ChatResponse(BaseModel):
    session_id: str
    reply: str
    model: str
    tier: str
    turn_count: int
    new_session: bool


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _build_market_context() -> str:
    """Compose the SYSTEM CONTEXT block from MC's live data."""
    positions = await db[SHARED_POSITIONS].find(
        {"state": {"$in": list(OPEN_STATES)}}, {"_id": 0},
    ).sort("updated_at", -1).to_list(15)

    lines: list[str] = ["=== SYSTEM CONTEXT (live MC data) ==="]
    lines.append(f"Time: {_now_iso()}")
    lines.append(f"Active signals: {len(positions)}")
    lines.append("")

    if positions:
        lines.append("OPEN SIGNALS:")
        for p in positions[:10]:
            stances = await db[SHARED_POSITION_STANCES].find(
                {"position_id": p["position_id"]}, {"_id": 0},
            ).sort("posted_at", -1).to_list(8)
            latest_by_brain: dict[str, dict] = {}
            for s in stances:
                latest_by_brain.setdefault(s["brain"], s)
            counts = {"long": 0, "short": 0, "abstain": 0}
            for s in latest_by_brain.values():
                counts[s["stance"]] = counts.get(s["stance"], 0) + 1
            lines.append(
                f"  - signal_id={p['position_id'][:8]} symbol={p['symbol']} "
                f"state={p['state']} direction={p.get('direction') or 'pending'} "
                f"votes(long/short/abstain)={counts['long']}/{counts['short']}/{counts['abstain']}"
            )
        lines.append("")

    snaps = await db[SHARED_INDICATOR_SNAPSHOTS].find(
        {}, {"_id": 0},
    ).limit(20).to_list(20)
    if snaps:
        lines.append("RECENT TECHNICALS (latest per symbol):")
        for s in snaps[:12]:
            ind = s.get("indicators") or {}
            close = ind.get("last_close")
            rsi = ind.get("rsi14")
            macd_hist = (ind.get("macd") or {}).get("hist")
            atr_pct = ind.get("atr14_pct")
            lines.append(
                f"  - {s['symbol']} ({s['source']} {s['tf']}): "
                f"close={close} rsi14={rsi} macd_hist={macd_hist} "
                f"atr14_pct={atr_pct}"
            )
    lines.append("=== END CONTEXT ===")
    return "\n".join(lines)


async def _load_session_history(session_id: str) -> list[dict]:
    rows = await db[PUBLIC_CHAT_MESSAGES].find(
        {"session_id": session_id}, {"_id": 0},
    ).sort("ts", 1).to_list(MAX_TURNS_PER_SESSION * 2 + 10)
    return rows


async def _persist_message(session_id: str, role: str, text: str) -> None:
    await db[PUBLIC_CHAT_MESSAGES].insert_one({
        "message_id": str(uuid.uuid4()),
        "session_id": session_id,
        "role": role,
        "text": text,
        "ts": _now_iso(),
    })


async def _prune_old_turns(session_id: str) -> None:
    """Keep at most MAX_TURNS_PER_SESSION user+assistant pairs (so 2x messages)."""
    cap = MAX_TURNS_PER_SESSION * 2
    total = await db[PUBLIC_CHAT_MESSAGES].count_documents(
        {"session_id": session_id},
    )
    if total <= cap:
        return
    to_drop = total - cap
    cursor = db[PUBLIC_CHAT_MESSAGES].find(
        {"session_id": session_id}, {"_id": 1},
    ).sort("ts", 1).limit(to_drop)
    ids = [doc["_id"] async for doc in cursor]
    if ids:
        await db[PUBLIC_CHAT_MESSAGES].delete_many({"_id": {"$in": ids}})


@router.post("/public/chat", response_model=ChatResponse)
async def post_chat(
    body: ChatRequest,
    caller: PublicCaller = Depends(public_trust_required),
):
    """Multi-turn grounded chat. Pro Max only.

    risedual.ai's responsibility:
      * Auth + tier check (we enforce pro_max here too as defense).
      * Deduct credits BEFORE calling MC (chat=1 credit per their docs).
      * Pass `session_id` to maintain the conversation; omit to start fresh.
    """
    if caller.tier != "pro_max":
        raise HTTPException(
            status_code=403,
            detail=(
                "Chat is Pro Max only. Upgrade the caller's tier or pass "
                "the correct X-RiseDual-User-Tier header."
            ),
        )
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

    session_id = body.session_id or f"sess-{uuid.uuid4().hex[:24]}"
    new_session = body.session_id is None

    # Load prior turns (replayed into a fresh LlmChat below).
    history = await _load_session_history(session_id) if not new_session else []

    # Build market context fresh on every turn — markets move.
    context = await _build_market_context()
    full_system = f"{SYSTEM_PROMPT}\n\n{context}"

    chat = LlmChat(
        api_key=api_key,
        session_id=session_id,
        system_message=full_system,
    ).with_model(LLM_PROVIDER, LLM_MODEL)

    # Replay history: re-send each prior user message so the LlmChat
    # instance accumulates the same context Claude saw before. We don't
    # re-call the LLM on prior turns — we just send the prior user
    # message and discard the response (it's already in our DB). For
    # cost-efficiency, we instead inject history via a synthetic
    # "prior conversation" string into the LATEST user prompt.
    history_summary = ""
    if history:
        snippets: list[str] = []
        for msg in history[-MAX_TURNS_PER_SESSION * 2:]:
            role = msg["role"].upper()
            t = (msg["text"] or "").strip()[:800]
            snippets.append(f"{role}: {t}")
        history_summary = (
            "\n\n=== PRIOR CONVERSATION (for context) ===\n"
            + "\n".join(snippets)
            + "\n=== END PRIOR ===\n"
        )

    user_text = (history_summary + "\nUSER: " + body.message).strip()

    try:
        reply = await chat.send_message(UserMessage(text=user_text))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(
            status_code=502, detail=f"LLM call failed: {e}",
        ) from e
    reply = (reply or "").strip()
    if not reply:
        raise HTTPException(status_code=502, detail="LLM returned empty reply")

    await _persist_message(session_id, "user", body.message)
    await _persist_message(session_id, "assistant", reply)
    await _prune_old_turns(session_id)

    turn_count = await db[PUBLIC_CHAT_MESSAGES].count_documents(
        {"session_id": session_id, "role": "user"},
    )
    return ChatResponse(
        session_id=session_id,
        reply=reply,
        model=f"{LLM_PROVIDER}:{LLM_MODEL}",
        tier=caller.tier,
        turn_count=turn_count,
        new_session=new_session,
    )


@router.get("/public/chat/history/{session_id}")
async def get_chat_history(
    session_id: str,
    caller: PublicCaller = Depends(public_trust_required),
):
    """Return the conversation tape for a session. Pro Max only.

    risedual.ai uses this when a user reloads the chat panel and needs
    to repaint the prior conversation."""
    if caller.tier != "pro_max":
        raise HTTPException(status_code=403, detail="Pro Max only")
    rows = await db[PUBLIC_CHAT_MESSAGES].find(
        {"session_id": session_id}, {"_id": 0},
    ).sort("ts", 1).to_list(200)
    return {
        "session_id": session_id,
        "messages": rows,
        "count": len(rows),
        "tier": caller.tier,
    }


@router.delete("/public/chat/history/{session_id}")
async def delete_chat_history(
    session_id: str,
    caller: PublicCaller = Depends(public_trust_required),
):
    """End a chat session (clear the tape). Pro Max only."""
    if caller.tier != "pro_max":
        raise HTTPException(status_code=403, detail="Pro Max only")
    r = await db[PUBLIC_CHAT_MESSAGES].delete_many({"session_id": session_id})
    return {"deleted": r.deleted_count, "session_id": session_id}
