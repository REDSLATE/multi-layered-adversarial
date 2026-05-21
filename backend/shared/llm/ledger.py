"""
Decision-trace ledger — every LLM call lands here.

Doctrine pin:
    This collection IS the moat. Each row is one brain reasoning
    step (prompt + response + provider + role + task + latency +
    auth-stamp). Over time these rows become:
      - The "decision trace" enterprise buyers ask for.
      - The replay substrate (re-run a paradox_record).
      - The training-data substrate for any future self-hosted
        brain that wants to learn from RISE_AI's own history.

    Best-effort write contract: a Mongo outage MUST NOT cause the
    brain's LLM call to fail. Exceptions in this module are caught
    by the kernel and logged.

Collection: `llm_calls` (see namespaces.py).
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from db import db
from namespaces import LLM_CALLS

# Hard cap on stored prompt/response size to keep individual docs
# from blowing past Mongo's 16MB doc limit and to keep replay queries
# fast. 200KB per side is generous — most chat turns are <50KB.
MAX_TEXT_BYTES = 200_000


def _clip(s: str) -> Dict[str, Any]:
    """Clip a string to MAX_TEXT_BYTES and report whether it was."""
    if s is None:
        return {"text": "", "bytes": 0, "truncated": False}
    encoded = s.encode("utf-8", errors="replace")
    if len(encoded) <= MAX_TEXT_BYTES:
        return {"text": s, "bytes": len(encoded), "truncated": False}
    truncated = encoded[:MAX_TEXT_BYTES].decode("utf-8", errors="ignore")
    return {"text": truncated, "bytes": MAX_TEXT_BYTES, "truncated": True}


async def record_llm_call(
    *,
    call_id: str,
    role: str,
    task: str,
    provider: str,
    model: str,
    prompt: str,
    response: str,
    ok: bool,
    error: Optional[str],
    usage: Optional[Dict[str, Any]],
    metadata: Optional[Dict[str, Any]],
    session_id: str,
    latency_ms: int,
) -> None:
    """Persist one LLM call to the ledger. Best-effort — errors are
    re-raised so the kernel's outer try/except can log them, but the
    kernel is contracted to ignore the exception."""
    prompt_info = _clip(prompt)
    response_info = _clip(response)
    now = datetime.now(timezone.utc).isoformat()
    doc = {
        "call_id": call_id,
        "session_id": session_id,
        "role": role,
        "task": task,
        "provider": provider,
        "model": model,
        "ok": ok,
        "error": error,
        "prompt": prompt_info["text"],
        "prompt_bytes": prompt_info["bytes"],
        "prompt_truncated": prompt_info["truncated"],
        "response": response_info["text"],
        "response_bytes": response_info["bytes"],
        "response_truncated": response_info["truncated"],
        "usage": usage or {},
        "metadata": metadata or {},
        "latency_ms": latency_ms,
        # The authority stamp lives in the LEDGER too — not just on
        # the kernel response. This way if a row is ever exported
        # outside MC, it carries its own "advisory-only" badge.
        "llm_authority": "ADVISORY_ONLY",
        "kernel_version": "0.1.0",
        "git_sha": os.environ.get("GIT_SHA", "unknown"),
        "created_at": now,
    }
    await db[LLM_CALLS].insert_one(doc)
