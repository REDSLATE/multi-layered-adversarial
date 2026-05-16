"""Public /chat — grounded RiseDualGPT for Pro Max.

Multi-turn chat using Claude Sonnet 4.5 via the official Anthropic
Python SDK (`anthropic.AsyncAnthropic`). Grounded in MC's open
positions + recent indicator snapshots — the brain doesn't make up
market data, it speaks about real positions and real indicators.

Doctrine:
  * Pro Max only. Free / starter / pro all get 403. risedual.ai
    handles credit deduction BEFORE calling MC; MC simply refuses
    callers below pro_max.
  * Session memory is persisted to MongoDB (`public_chat_messages`)
    keyed by `session_id`. Multi-turn survives MC restarts because we
    replay history as a `messages=[…]` list on every request. The
    Anthropic Messages API is stateless — we send the full window each
    call.
  * Hard cap of 25 turns per session (= 50 messages). Beyond that the
    oldest turns drop off (rolling window) — keeps token cost bounded.
  * Refuses to talk about anything outside trading, markets, technical
    analysis, or RiseDual's own outputs (system prompt enforces).

History (2026-02-16): refactored away from emergentintegrations to
the vendor SDK per the latest integration_playbook_expert_v2 guidance.
Key migration notes:
  - LlmChat(...).with_model().send_message(...) → AsyncAnthropic().messages.create(model=..., system=..., messages=[...]).
  - The legacy implementation pasted prior turns into a synthetic
    "PRIOR CONVERSATION" preamble on the LATEST user message. The
    vendor SDK accepts a proper alternating user/assistant messages
    list, so we now build that natively — better fidelity, cheaper
    tokens, and `stop_reason` visibility comes for free.
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
    import anthropic
    from anthropic import AsyncAnthropic
except ImportError:  # pragma: no cover — defensive only
    anthropic = None  # type: ignore[assignment]
    AsyncAnthropic = None  # type: ignore[assignment]


router = APIRouter(tags=["public"])

# Model selection — date-stamped snapshot for stability. Operator can
# override with CLAUDE_MODEL_ID in backend/.env if a newer version
# ships and we want to opt in without a code change.
LLM_PROVIDER = "anthropic"
LLM_MODEL = os.environ.get("CLAUDE_MODEL_ID", "claude-sonnet-4-5-20250929")

# Per-response output cap. Keeps cost predictable and surfaces
# `stop_reason == "max_tokens"` clearly when the model would otherwise
# run long.
MAX_OUTPUT_TOKENS = int(os.environ.get("CLAUDE_MAX_OUTPUT_TOKENS", "1024"))

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

# Module-level client. Lazily instantiated on first call so the import
# of this module doesn't fail when ANTHROPIC_API_KEY is missing (the
# endpoint will return 503 instead, same as the legacy behavior).
_CLIENT: Optional["AsyncAnthropic"] = None


def _get_client() -> "AsyncAnthropic":
    """Return a singleton AsyncAnthropic client. Raises 503 if the SDK
    isn't installed or the API key is missing."""
    global _CLIENT
    if _CLIENT is not None:
        return _CLIENT
    if AsyncAnthropic is None:
        raise HTTPException(
            status_code=503,
            detail="LLM integration not installed (anthropic SDK missing)",
        )
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise HTTPException(
            status_code=503,
            detail="LLM not configured (ANTHROPIC_API_KEY unset in backend/.env)",
        )
    # max_retries: SDK retries 429/5xx with exponential backoff internally.
    _CLIENT = AsyncAnthropic(api_key=key, max_retries=2)
    return _CLIENT


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
    stop_reason: Optional[str] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None


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


def _history_to_messages(history: list[dict]) -> list[dict]:
    """Translate persisted history rows into the Anthropic Messages
    API shape. Skips any row whose role isn't user/assistant (defensive
    against schema drift). Trims content to MongoDB's stored value as-is
    — we don't re-truncate here.
    """
    msgs: list[dict] = []
    for row in history[-(MAX_TURNS_PER_SESSION * 2):]:
        role = row.get("role")
        if role not in ("user", "assistant"):
            continue
        text = (row.get("text") or "").strip()
        if not text:
            continue
        msgs.append({"role": role, "content": text})
    # The Anthropic API requires that the FIRST message be a user
    # message. If history starts with an assistant turn (shouldn't
    # happen but defend), drop leading assistant rows.
    while msgs and msgs[0]["role"] != "user":
        msgs.pop(0)
    return msgs


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


def _extract_text(response) -> str:
    """Concatenate every `text`-typed content block on the response.
    Anthropic responses can contain multiple blocks (text, tool_use, …);
    for our single-text-out endpoint we only care about text."""
    chunks: list[str] = []
    for block in getattr(response, "content", []) or []:
        if getattr(block, "type", None) == "text":
            chunks.append(getattr(block, "text", ""))
    return "".join(chunks).strip()


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

    client = _get_client()

    session_id = body.session_id or f"sess-{uuid.uuid4().hex[:24]}"
    new_session = body.session_id is None

    # Replay prior turns from MongoDB as a proper Anthropic messages
    # list (alternating user/assistant). The legacy implementation
    # stuffed history into the LATEST user message — the vendor SDK
    # accepts native multi-turn shape, so we feed it directly.
    history = await _load_session_history(session_id) if not new_session else []
    messages = _history_to_messages(history)

    # Build market context fresh on every turn — markets move. The
    # context is appended to the SYSTEM prompt, not pasted into the
    # user message, so the model treats it as the operator-set frame
    # rather than user-supplied data.
    context = await _build_market_context()
    full_system = f"{SYSTEM_PROMPT}\n\n{context}"

    # Current user turn.
    messages.append({"role": "user", "content": body.message})

    # Call Claude. SDK handles 429/5xx retries internally up to
    # max_retries=2; remaining errors surface here.
    try:
        response = await client.messages.create(
            model=LLM_MODEL,
            max_tokens=MAX_OUTPUT_TOKENS,
            system=full_system,
            messages=messages,
        )
    except anthropic.RateLimitError as e:  # type: ignore[union-attr]
        raise HTTPException(
            status_code=429,
            detail="Anthropic rate limit exceeded; please retry shortly.",
        ) from e
    except anthropic.APIConnectionError as e:  # type: ignore[union-attr]
        raise HTTPException(
            status_code=503,
            detail="Unable to reach Anthropic API; try again shortly.",
        ) from e
    except anthropic.APIStatusError as e:  # type: ignore[union-attr]
        raise HTTPException(
            status_code=502,
            detail=f"Anthropic API error ({getattr(e, 'status_code', '?')})",
        ) from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(
            status_code=502, detail=f"LLM call failed: {e}",
        ) from e

    reply = _extract_text(response)
    if not reply:
        raise HTTPException(status_code=502, detail="LLM returned empty reply")

    await _persist_message(session_id, "user", body.message)
    await _persist_message(session_id, "assistant", reply)
    await _prune_old_turns(session_id)

    turn_count = await db[PUBLIC_CHAT_MESSAGES].count_documents(
        {"session_id": session_id, "role": "user"},
    )

    usage = getattr(response, "usage", None)
    return ChatResponse(
        session_id=session_id,
        reply=reply,
        model=f"{LLM_PROVIDER}:{LLM_MODEL}",
        tier=caller.tier,
        turn_count=turn_count,
        new_session=new_session,
        stop_reason=getattr(response, "stop_reason", None),
        input_tokens=getattr(usage, "input_tokens", None) if usage else None,
        output_tokens=getattr(usage, "output_tokens", None) if usage else None,
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
