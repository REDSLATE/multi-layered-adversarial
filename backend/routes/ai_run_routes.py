"""
RISE_AI Unified Entry — `POST /api/ai/run`
==========================================

Doctrine pin (2026-02-XX):
    This is the "single front door" for ad-hoc RISE_AI queries.
    Internally it dispatches into the PRODUCTION services that
    are already built:

        chat     → kernel.call(role="auditor", task="chat")
        reason   → kernel.call(role="strategist", task="reason")
        code     → kernel.call(role="strategist", task="code_task")
        trade    → READ-ONLY observation pulled from paradox_*
                   collections. NEVER calls /api/execution/submit.
                   NEVER places an order. Trade mode is a
                   reporter, not an executor.
        research → kernel.call(role="memory", task="research")

    Every call still flows through `shared.llm.llm_kernel`, so:
      * It lands in the `llm_calls` ledger
      * It's gradable from /admin/llm-ledger
      * It carries `llm_authority="ADVISORY_ONLY"`

Safety check:
    A REAL safety check (not a 3-string blocklist). Rejects:
      * Execution-intent phrases ("place order", "buy now", etc.)
      * Doctrine-modify phrases ("disable gate", "bypass", "override")
      * Auth-tampering phrases ("steal password", "malware")
    Blocked prompts return `safety_status="blocked"` and a tame
    message instead of being sent to any LLM.
"""
from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from auth import get_current_user
from db import db
from namespaces import PARADOX_CANDIDATES, PARADOX_RECORDS
from shared.llm import llm_kernel

log = logging.getLogger("risedual.ai_run")

router = APIRouter(prefix="/ai", tags=["rise-ai-run"])


VALID_MODES = ("chat", "reason", "code", "trade", "research", "memory", "status")


# Valid role overrides — operator can force a specific role for a
# given mode (e.g. "chat" + role=opponent to get adversarial chat).
VALID_ROLE_OVERRIDES = (
    "strategist", "governor", "opponent", "memory", "auditor", "executor",
)


# ─── Safety check ─────────────────────────────────────────────────────


# Phrases that, if found in the prompt, hard-block the request.
# Categorised so the response can name what tripped.
_SAFETY_PATTERNS: Dict[str, list[re.Pattern]] = {
    "execution_intent": [
        re.compile(r"\bplace (an? )?(market |limit )?order\b", re.IGNORECASE),
        re.compile(r"\bbuy (now|immediately|right now)\b", re.IGNORECASE),
        re.compile(r"\bsell (now|immediately|right now)\b", re.IGNORECASE),
        re.compile(r"\bexecute (the )?trade\b", re.IGNORECASE),
        re.compile(r"\bfire (the )?order\b", re.IGNORECASE),
        re.compile(r"\bsubmit (an? |the )?intent\b", re.IGNORECASE),
    ],
    "doctrine_tamper": [
        re.compile(r"\bdisable (the )?(gate|roadguard|kill[ -]?switch)\b", re.IGNORECASE),
        re.compile(r"\bbypass (the )?(gate|roadguard|safety|veto)\b", re.IGNORECASE),
        re.compile(r"\boverride (the )?(opponent|veto|hold)\b", re.IGNORECASE),
        re.compile(r"\bturn off (the )?(safety|kill[ -]?switch)\b", re.IGNORECASE),
    ],
    "auth_tamper": [
        re.compile(r"\bsteal (the )?password\b", re.IGNORECASE),
        re.compile(r"\bmalware\b", re.IGNORECASE),
        re.compile(r"\bexploit (the )?bank\b", re.IGNORECASE),
        re.compile(r"\bdrain (the )?account\b", re.IGNORECASE),
    ],
}


def safety_check(prompt: str) -> Dict[str, Any]:
    """Return {status, category|None, matched|None}."""
    if not prompt:
        return {"status": "allowed", "category": None, "matched": None}
    for category, patterns in _SAFETY_PATTERNS.items():
        for pat in patterns:
            m = pat.search(prompt)
            if m:
                return {
                    "status": "blocked",
                    "category": category,
                    "matched": m.group(0),
                }
    return {"status": "allowed", "category": None, "matched": None}


# ─── Mode → role/task mapping ────────────────────────────────────────


_MODE_ROUTING = {
    "chat":     {"role": "auditor",    "task": "ai_run_chat"},
    "reason":   {"role": "strategist", "task": "ai_run_reason"},
    "code":     {"role": "strategist", "task": "ai_run_code"},
    "research": {"role": "memory",     "task": "ai_run_research"},
    "memory":   {"role": "memory",     "task": "ai_run_memory_recall"},
}


_MODE_SYSTEM_PROMPTS = {
    "chat": (
        "You are RISE_AI in conversational mode. Be concise, factual, "
        "and clear. ADVISORY ONLY — you do not place trades."
    ),
    "reason": (
        "You are RISE_AI in structured-reasoning mode. Break the "
        "problem into facts, assumptions, risks, and actionable next "
        "steps. ADVISORY ONLY."
    ),
    "code": (
        "You are RISE_AI in code-assist mode. Read the question, "
        "propose a small, testable change. Do NOT produce code that "
        "bypasses execution gates, modifies doctrine, or alters the "
        "kill switch. ADVISORY ONLY."
    ),
    "research": (
        "You are RISE_AI in research mode. Synthesize what you know, "
        "name your assumptions, and flag uncertainty explicitly. "
        "ADVISORY ONLY."
    ),
    "memory": (
        "You are RISE_AI in memory-recall mode. Answer from what the "
        "system has seen and logged. If you don't have evidence in "
        "the ledger, say so explicitly. ADVISORY ONLY."
    ),
}


# ─── Status mode (read-only system snapshot) ──────────────────────────


async def _status_observation() -> Dict[str, Any]:
    """READ-ONLY system snapshot. No LLM call. No mutation. Returns
    a quick overview of the kernel/coordinator/ledger state so the
    operator can ask 'how is RISE_AI right now?' without leaving
    the chat surface."""
    from namespaces import (
        LLM_CALLS,
        LLM_DISTILLATION_QUEUE,
        LLM_PROVIDER_STATE,
    )
    from shared.llm.routing_policy import DEFAULT_PROMOTION_STATE

    candidate_counts = {}
    for s in ("candidate", "pending_snapshot", "evaluated", "risk_blocked"):
        candidate_counts[s] = await db[PARADOX_CANDIDATES].count_documents({"status": s})

    paradox_record_count = await db[PARADOX_RECORDS].count_documents(
        {"evaluation_kind": "paradox_v0_evaluation"},
    )
    llm_call_count = await db[LLM_CALLS].count_documents({})
    distill_count = await db[LLM_DISTILLATION_QUEUE].count_documents({"consumed_at": None})

    # Promotion states (defaults overlaid with operator settings)
    promo = dict(DEFAULT_PROMOTION_STATE)
    async for d in db[LLM_PROVIDER_STATE].find({}, {"_id": 0, "provider": 1, "state": 1}):
        if d.get("provider") in promo and d.get("state"):
            promo[d["provider"]] = d["state"]

    return {
        "summary": (
            f"Paradox candidates: {candidate_counts['candidate']} ready, "
            f"{candidate_counts['evaluated']} evaluated, "
            f"{candidate_counts['risk_blocked']} risk-blocked. "
            f"LLM ledger: {llm_call_count} calls. "
            f"Distillation queue: {distill_count} winners pending."
        ),
        "candidates": candidate_counts,
        "paradox_evaluations": paradox_record_count,
        "llm_calls_total": llm_call_count,
        "distillation_pending": distill_count,
        "provider_promotion": promo,
    }


# ─── Trade mode (read-only observation) ───────────────────────────────


async def _trade_observation(prompt: str) -> Dict[str, Any]:
    """Trade mode NEVER calls the LLM kernel and NEVER posts an
    order. It returns a SNAPSHOT of the most-recent paradox state
    so the operator can see what the brains are looking at.

    The `prompt` is logged but otherwise ignored — this is a
    reporter, not a strategist."""
    # Most-recent 5 candidates (any status).
    candidates = []
    async for d in (
        db[PARADOX_CANDIDATES]
        .find({}, {"_id": 0, "candidate_id": 1, "symbol": 1, "status": 1,
                   "reason": 1, "created_at": 1})
        .sort("created_at", -1).limit(5)
    ):
        if isinstance(d.get("created_at"), datetime):
            d["created_at"] = d["created_at"].isoformat()
        candidates.append(d)

    # Most-recent 5 paradox_v0_evaluation rows.
    evaluations = []
    async for d in (
        db[PARADOX_RECORDS]
        .find(
            {"evaluation_kind": "paradox_v0_evaluation"},
            {"_id": 0, "evaluation_id": 1, "symbol": 1, "verdict": 1,
             "status": 1, "created_at": 1},
        )
        .sort("created_at", -1).limit(5)
    ):
        if isinstance(d.get("created_at"), datetime):
            d["created_at"] = d["created_at"].isoformat()
        evaluations.append(d)

    return {
        "summary": (
            "RISE_AI is in observation mode. "
            f"{len(candidates)} recent candidates, "
            f"{len(evaluations)} recent evaluations. "
            "No execution authority from this endpoint."
        ),
        "recent_candidates": candidates,
        "recent_evaluations": evaluations,
        "note": (
            "Trade mode is READ-ONLY. To act on an evaluation, "
            "use the human-gated promotion endpoint (planned) — "
            "this endpoint will never trigger an order."
        ),
    }


# ─── Request / response schemas ──────────────────────────────────────


class AIRequest(BaseModel):
    user_id: str = Field(default="default", max_length=120)
    prompt: str = Field(..., min_length=1, max_length=8000)
    mode: str = Field(default="chat")
    session_id: Optional[str] = Field(default=None, max_length=120)
    role_override: Optional[str] = Field(default=None, max_length=40)


class AIResponse(BaseModel):
    request_id: str
    mode: str
    answer: str
    safety_status: str
    safety_category: Optional[str] = None
    safety_matched: Optional[str] = None
    call_id: Optional[str] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    latency_ms: Optional[int] = None
    llm_authority: str = "ADVISORY_ONLY"
    created_at: str
    extra: Optional[Dict[str, Any]] = None


# ─── Endpoint ────────────────────────────────────────────────────────


@router.post("/run", response_model=AIResponse)
async def ai_run(
    body: AIRequest,
    user: dict = Depends(get_current_user),
) -> AIResponse:
    """Unified RISE_AI entry. All modes route through the production
    LLM kernel (which ledgers + stamps ADVISORY_ONLY), except
    `trade` which is a read-only observation surface.
    """
    if body.mode not in VALID_MODES:
        raise HTTPException(
            status_code=400,
            detail=f"mode {body.mode!r} not in {list(VALID_MODES)}",
        )

    request_id = str(uuid.uuid4())
    now_iso = datetime.now(timezone.utc).isoformat()

    # 1. Safety check FIRST. Block at this layer; do NOT spend
    # tokens on a blocked prompt.
    safety = safety_check(body.prompt)
    if safety["status"] == "blocked":
        return AIResponse(
            request_id=request_id,
            mode=body.mode,
            answer=(
                "Request blocked by safety policy. "
                f"Category: {safety['category']}. "
                "RISE_AI does not assist with execution intent, "
                "doctrine tampering, or auth tampering. "
                "Rephrase the question as observation or analysis."
            ),
            safety_status="blocked",
            safety_category=safety["category"],
            safety_matched=safety["matched"],
            call_id=None,
            llm_authority="ADVISORY_ONLY",
            created_at=now_iso,
        )

    # 2. Trade mode is read-only observation — no LLM call.
    if body.mode == "trade":
        obs = await _trade_observation(body.prompt)
        return AIResponse(
            request_id=request_id,
            mode="trade",
            answer=obs["summary"],
            safety_status="allowed",
            call_id=None,
            llm_authority="ADVISORY_ONLY",
            created_at=now_iso,
            extra={
                "recent_candidates": obs["recent_candidates"],
                "recent_evaluations": obs["recent_evaluations"],
                "note": obs["note"],
                "answer_source": "paradox_records",
            },
        )

    # 2b. Status mode is read-only observation — no LLM call.
    if body.mode == "status":
        st = await _status_observation()
        return AIResponse(
            request_id=request_id,
            mode="status",
            answer=st["summary"],
            safety_status="allowed",
            call_id=None,
            llm_authority="ADVISORY_ONLY",
            created_at=now_iso,
            extra={
                "candidates": st["candidates"],
                "paradox_evaluations": st["paradox_evaluations"],
                "llm_calls_total": st["llm_calls_total"],
                "distillation_pending": st["distillation_pending"],
                "provider_promotion": st["provider_promotion"],
                "answer_source": "static_system_data",
            },
        )

    # 3. Other modes route through the kernel.
    routing = _MODE_ROUTING[body.mode]
    # Operator can override role within an allowed set; otherwise use
    # the mode's default role.
    if body.role_override:
        if body.role_override not in VALID_ROLE_OVERRIDES:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"role_override {body.role_override!r} not in "
                    f"{list(VALID_ROLE_OVERRIDES)}"
                ),
            )
        role = body.role_override
    else:
        role = routing["role"]
    system = _MODE_SYSTEM_PROMPTS[body.mode]
    result = await llm_kernel.call(
        role=role,
        task=routing["task"],
        prompt=body.prompt,
        system=system,
        session_id=body.session_id or f"ai_run_{request_id}",
        metadata={
            "ai_run_request_id": request_id,
            "ai_run_user_id": body.user_id,
            "ai_run_mode": body.mode,
            "ai_run_role_override": body.role_override,
            "operator_email": user.get("email", "unknown"),
        },
    )

    answer = result.get("response") or ""
    if not answer and not result.get("ok"):
        answer = (
            "LLM call failed. Check the LLM Ledger for the error "
            f"on call_id {result.get('call_id')!r}."
        )

    return AIResponse(
        request_id=request_id,
        mode=body.mode,
        answer=answer,
        safety_status="allowed",
        call_id=result.get("call_id"),
        provider=result.get("provider"),
        model=result.get("model"),
        latency_ms=result.get("latency_ms"),
        llm_authority="ADVISORY_ONLY",
        created_at=now_iso,
        extra={"answer_source": "llm_kernel", "role": role},
    )
