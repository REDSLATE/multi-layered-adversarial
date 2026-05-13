"""Hypothesis engine — on-demand dual-narrative analysis for a ticker.

Operator clicks 'Analyze NVDA' → MC runs TWO LLM calls in parallel:

  * STRATEGIST — the brain holding the Executor seat plays this role.
    Generates: catalysts (bull case), short-term price target (1-2w),
    medium-term target (1-3mo), and a tight investment thesis.

  * AUDITOR — the brain holding the Auditor seat plays this role.
    Generates: risk flags, what-could-go-wrong scenarios, and explicit
    kill-switch triggers (price/indicator levels that invalidate the
    thesis).

Doctrine:
  * Analysis is ANCHORED in MC's live market context (latest indicator
    snapshots, any open positions, recent intents on the symbol). LLMs
    are told not to invent prices or news.
  * If a seat is empty, the corresponding role falls back to a generic
    skeptic/strategist voice (no brain persona).
  * Two different LLM providers by default (Claude for Strategist, GPT
    for Auditor) — model diversity = different blind spots. Mirrors the
    user's existing risedual.ai "Hypothesis" three-model consensus.
  * Every analysis is audit-logged to `hypothesis_analyses`. The 30-min
    cache lives in the BROWSER (client-side, per user instruction); the
    server doesn't memoize.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from auth import get_current_user
from db import db
from namespaces import (
    HYPOTHESIS_ANALYSES,
    SHARED_INDICATOR_SNAPSHOTS,
    SHARED_INTENTS,
    SHARED_POSITIONS,
)
from shared.auditor_seat import get_auditor_holder
from shared.executor_seat import get_executor_holder

try:
    from emergentintegrations.llm.chat import LlmChat, UserMessage
except ImportError:  # noqa: F401
    LlmChat = None
    UserMessage = None


router = APIRouter(prefix="/hypothesis", tags=["hypothesis"])


STRATEGIST_PROVIDER = "anthropic"
STRATEGIST_MODEL = "claude-sonnet-4-5-20250929"

# Gemini 3 Flash for the Auditor — fast (~3-5s typical) + different
# blind spots vs Claude, which is the whole point of dual-model analysis.
# Avoids GPT-5's reasoning-mode latency (~50s on a fresh call) that blew
# past the ingress 60s timeout in initial testing.
AUDITOR_PROVIDER = "gemini"
AUDITOR_MODEL = "gemini-3-flash-preview"


# Per-brain persona blurbs — injected into the role system prompt so the
# voice carries the doctrine.
BRAIN_PERSONA: dict[str, str] = {
    "alpha":    "ALPHA — disciplined, evidence-first. Trader's eye. Prefers crisp catalysts over big-picture stories.",
    "camaro":   "CAMARO — aggressive challenger. Likes high-conviction asymmetric setups but always defines invalidation.",
    "chevelle": "CHEVELLE — governor's voice. Cautious, structural, thinks in regimes and macro context.",
    "redeye":   "REDEYE — the opponent. Pessimistic by design. Looks for the failure mode others miss.",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ───────────────────────────── context builder ─────────────────────────────

async def _build_symbol_context(symbol: str) -> dict:
    """Pull live MC data anchoring the analysis. LLMs are instructed to
    only reference values present in this block — no fabrication."""
    sym = symbol.upper()
    snap = await db[SHARED_INDICATOR_SNAPSHOTS].find_one(
        {"symbol": sym}, {"_id": 0},
        sort=[("captured_at", -1)],
    )
    intents = await db[SHARED_INTENTS].find(
        {"symbol": sym}, {"_id": 0, "intent_id": 1, "stack": 1, "action": 1,
                          "confidence": 1, "rationale": 1, "posted_at": 1}
    ).sort("posted_at", -1).to_list(8)
    positions = await db[SHARED_POSITIONS].find(
        {"symbol": sym, "state": {"$in": ["open", "managing"]}},
        {"_id": 0, "position_id": 1, "direction": 1, "state": 1, "updated_at": 1},
    ).sort("updated_at", -1).to_list(5)
    return {
        "symbol": sym,
        "now": _now_iso(),
        "indicator_snapshot": snap,
        "recent_intents": intents,
        "open_positions": positions,
        "has_market_context": snap is not None,
    }


# ───────────────────────────── prompts ─────────────────────────────

STRATEGIST_SYSTEM_BASE = (
    "You are the STRATEGIST in RiseDual's adversarial two-AI hypothesis "
    "engine. You build the bull/bear case FOR the proposed direction. "
    "Your sibling — the AUDITOR — will independently attack the same "
    "ticker. You don't read their work; you state your case cleanly.\n\n"
    "RULES:\n"
    "1. Anchor every claim in the SYMBOL_CONTEXT JSON. Do NOT invent "
    "prices, indicators, news headlines, or fundamentals.\n"
    "2. If a piece of data isn't in the context, say so explicitly "
    "(\"no recent indicator data\") — DO NOT fabricate.\n"
    "3. Output STRICT JSON ONLY — no prose preamble, no markdown fences. "
    "The schema is enforced.\n"
    "4. Targets are stated as percentage ranges from current levels with "
    "an explicit base case (e.g., '+3% to +7%, base +5%').\n"
    "5. The Investment Thesis is 2-4 sentences max. Plain English.\n"
    "6. Catalysts are concrete, specific events / data / structural "
    "factors — not vague platitudes. 3-6 bullet items.\n\n"
    "REQUIRED JSON SHAPE:\n"
    "{\n"
    '  "direction": "BUY" | "SELL" | "HOLD",\n'
    '  "confidence_pct": 0..100,\n'
    '  "short_term_target": "1-2w: +X% to +Y% (base +Z%)" or similar,\n'
    '  "medium_term_target": "1-3mo: ...",\n'
    '  "investment_thesis": "2-4 sentences",\n'
    '  "catalysts": ["…", "…", "…"]\n'
    "}\n"
)

AUDITOR_SYSTEM_BASE = (
    "You are the AUDITOR in RiseDual's adversarial two-AI hypothesis "
    "engine. Your job is to attack the trade. You don't read the "
    "Strategist's work; you find the failure modes independently.\n\n"
    "RULES:\n"
    "1. Anchor every risk in the SYMBOL_CONTEXT JSON. If a risk isn't "
    "supported by anything in the context, mark it as a 'background "
    "risk' rather than asserting it as imminent.\n"
    "2. NO fabrication. If the context shows no recent indicator data, "
    "say so — don't invent RSI or MACD values.\n"
    "3. Output STRICT JSON ONLY — no prose preamble, no markdown fences.\n"
    "4. Risk flags are concrete: rates / valuation / regulatory / "
    "execution / macro / sector-rotation — 3-6 items.\n"
    "5. Kill-switch triggers are EXPLICIT and TESTABLE — price levels, "
    "indicator thresholds, time-based stops — not vague feelings.\n\n"
    "REQUIRED JSON SHAPE:\n"
    "{\n"
    '  "verdict": "ACCEPTABLE" | "BORDERLINE" | "UNACCEPTABLE",\n'
    '  "risk_flags": ["…", "…", "…"],\n'
    '  "what_could_go_wrong": ["scenario 1", "scenario 2"],\n'
    '  "kill_switch_triggers": ["exit if SPY breaks below 4200",\n'
    '                          "exit if VIX > 28 for 2 sessions"]\n'
    "}\n"
)


def _persona_block(brain: Optional[str], role: str) -> str:
    if not brain:
        return (
            f"\nROLE PERSONA: No brain currently holds the {role} seat. "
            "Use a neutral, professional analyst voice.\n"
        )
    blurb = BRAIN_PERSONA.get(brain, brain.upper())
    return (
        f"\nROLE PERSONA: You are speaking AS the brain currently "
        f"occupying the {role} seat — {blurb} Stay in character but "
        "stay grounded in the data.\n"
    )


def _build_user_prompt(symbol: str, context: dict) -> str:
    return (
        f"SYMBOL: {symbol}\n\n"
        f"SYMBOL_CONTEXT (live MC data — anchor to this only):\n"
        f"{json.dumps(context, default=str, indent=2)}\n\n"
        "Produce your JSON response now. JSON ONLY — no preamble, no "
        "markdown fences."
    )


# ───────────────────────────── LLM execution ─────────────────────────────

def _parse_json_lenient(raw: str) -> dict:
    """Models occasionally wrap JSON in ```json ... ``` fences. Strip them."""
    if not raw:
        return {}
    text = raw.strip()
    # Strip markdown fences if present.
    fence = re.match(r"^```(?:json)?\s*(.*?)```\s*$", text, re.DOTALL | re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Try to locate the first { ... } block.
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
    return {"_raw": text[:1500], "_parse_error": True}


async def _llm_role_call(
    *,
    api_key: str,
    role: str,                       # "strategist" or "auditor"
    provider: str,
    model: str,
    brain: Optional[str],
    symbol: str,
    context: dict,
) -> dict:
    base = STRATEGIST_SYSTEM_BASE if role == "strategist" else AUDITOR_SYSTEM_BASE
    system = base + _persona_block(brain, role)
    user_prompt = _build_user_prompt(symbol, context)
    chat = LlmChat(
        api_key=api_key,
        session_id=f"hyp-{role}-{uuid.uuid4().hex[:12]}",
        system_message=system,
    ).with_model(provider, model)
    try:
        text = await chat.send_message(UserMessage(text=user_prompt))
    except Exception as e:  # noqa: BLE001
        return {
            "_error": f"{role} LLM call failed: {e}",
            "model": f"{provider}:{model}",
            "brain": brain,
        }
    parsed = _parse_json_lenient(text or "")
    parsed["_meta"] = {
        "model": f"{provider}:{model}",
        "brain": brain,
        "generated_at": _now_iso(),
    }
    return parsed


# ───────────────────────────── routes ─────────────────────────────

_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")


class AnalyzeBody(BaseModel):
    symbol: str = Field(..., min_length=1, max_length=10)

    @field_validator("symbol")
    @classmethod
    def _norm(cls, v: str) -> str:
        v = (v or "").strip().upper()
        if not _SYMBOL_RE.match(v):
            raise ValueError(
                "symbol must be 1-10 chars, uppercase letters/digits/. /- only"
            )
        return v


class AnalyzeResponse(BaseModel):
    analysis_id: str
    symbol: str
    generated_at: str
    strategist: dict
    auditor: dict
    context: dict
    seats: dict


@router.post("/analyze", response_model=AnalyzeResponse)
async def analyze(
    body: AnalyzeBody,
    user: dict = Depends(get_current_user),  # noqa: B008
):
    """Operator-triggered. Runs Strategist + Auditor analysis in parallel.

    The 30-min cache lives in the operator's browser (per spec). This
    endpoint ALWAYS generates a fresh analysis — no server-side memo.
    """
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

    symbol = body.symbol
    context = await _build_symbol_context(symbol)

    strategist_brain = await get_executor_holder()
    auditor_brain = await get_auditor_holder()

    # Run both LLM calls concurrently — typical wall time ~3-6s instead of ~10.
    strategist_task = _llm_role_call(
        api_key=api_key,
        role="strategist",
        provider=STRATEGIST_PROVIDER,
        model=STRATEGIST_MODEL,
        brain=strategist_brain,
        symbol=symbol,
        context=context,
    )
    auditor_task = _llm_role_call(
        api_key=api_key,
        role="auditor",
        provider=AUDITOR_PROVIDER,
        model=AUDITOR_MODEL,
        brain=auditor_brain,
        symbol=symbol,
        context=context,
    )
    strategist, auditor = await asyncio.gather(strategist_task, auditor_task)

    analysis_id = str(uuid.uuid4())
    now = _now_iso()
    row: dict[str, Any] = {
        "analysis_id": analysis_id,
        "symbol": symbol,
        "generated_at": now,
        "requested_by": user.get("email"),
        "strategist": strategist,
        "auditor": auditor,
        "context_summary": {
            "has_market_context": context.get("has_market_context"),
            "open_positions": len(context.get("open_positions") or []),
            "recent_intents": len(context.get("recent_intents") or []),
        },
        "seats": {
            "strategist_brain": strategist_brain,
            "auditor_brain": auditor_brain,
        },
    }
    await db[HYPOTHESIS_ANALYSES].insert_one(row)

    return AnalyzeResponse(
        analysis_id=analysis_id,
        symbol=symbol,
        generated_at=now,
        strategist=strategist,
        auditor=auditor,
        context=context,
        seats={
            "strategist_brain": strategist_brain,
            "auditor_brain": auditor_brain,
        },
    )


@router.get("/recent")
async def list_recent(
    limit: int = 25,
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    rows = (
        await db[HYPOTHESIS_ANALYSES]
        .find({}, {"_id": 0})
        .sort("generated_at", -1)
        .to_list(min(max(1, limit), 200))
    )
    return {"items": rows, "count": len(rows)}
