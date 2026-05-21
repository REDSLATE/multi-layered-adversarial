"""
Eval harness — compares a candidate provider against the current
primary on a held-out prompt set.

Doctrine pin:
    The promotion path SHADOW → ADVISOR → PRIMARY is gated on this.
    The harness runs each prompt through BOTH:
      (a) the role's current PRIMARY provider, and
      (b) the candidate provider (e.g. `self_trained`)

    It records both answers, scores their similarity / agreement,
    and writes a row to `llm_eval_runs`. The operator inspects
    those rows and decides whether to promote.

    The agreement score is intentionally crude in this scaffold
    (token-overlap Jaccard). A future revision can swap in semantic
    similarity (embeddings) once the embedding adapter exists.
"""
from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from db import db
from namespaces import LLM_EVAL_RUNS

logger = logging.getLogger("risedual.llm_kernel.eval_harness")


async def evaluate_candidate(
    *,
    role: str,
    task: str,
    candidate_provider: str,
    candidate_model: Optional[str] = None,
    primary_provider: Optional[str] = None,
    primary_model: Optional[str] = None,
    eval_set: List[Dict[str, str]],
    note: str = "",
) -> Dict[str, Any]:
    """Run each prompt in `eval_set` through both providers and
    score agreement.

    `eval_set` is a list of {"id": ..., "prompt": ...} entries.

    Returns the aggregate run summary AND persists the full per-
    prompt detail to `llm_eval_runs`.
    """
    from shared.llm.kernel import llm_kernel  # late import to dodge cycle

    run_id = str(uuid.uuid4())
    rows: List[Dict[str, Any]] = []

    for entry in eval_set:
        pid = entry.get("id") or str(uuid.uuid4())
        prompt = entry.get("prompt", "")

        # Primary call: kernel picks normally.
        primary_resp = await llm_kernel.call(
            role=role, task=task, prompt=prompt,
            provider_override=primary_provider,
            model_override=primary_model,
            metadata={"eval_run_id": run_id, "eval_role": "primary", "eval_prompt_id": pid},
        )

        # Candidate call: force the candidate.
        candidate_resp = await llm_kernel.call(
            role=role, task=task, prompt=prompt,
            provider_override=candidate_provider,
            model_override=candidate_model,
            metadata={"eval_run_id": run_id, "eval_role": "candidate", "eval_prompt_id": pid},
        )

        agreement = _jaccard_tokens(
            primary_resp.get("response", ""),
            candidate_resp.get("response", ""),
        )

        rows.append({
            "prompt_id": pid,
            "prompt": prompt,
            "primary": {
                "provider": primary_resp.get("provider"),
                "model": primary_resp.get("model"),
                "response": primary_resp.get("response"),
                "ok": primary_resp.get("ok"),
                "latency_ms": primary_resp.get("latency_ms"),
                "call_id": primary_resp.get("call_id"),
            },
            "candidate": {
                "provider": candidate_resp.get("provider"),
                "model": candidate_resp.get("model"),
                "response": candidate_resp.get("response"),
                "ok": candidate_resp.get("ok"),
                "latency_ms": candidate_resp.get("latency_ms"),
                "call_id": candidate_resp.get("call_id"),
            },
            "agreement": agreement,
        })

    summary = _summarize(rows)
    doc = {
        "run_id": run_id,
        "role": role,
        "task": task,
        "candidate_provider": candidate_provider,
        "candidate_model": candidate_model,
        "primary_provider": primary_provider,
        "primary_model": primary_model,
        "note": note or None,
        "n": len(rows),
        "summary": summary,
        "rows": rows,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "llm_authority": "ADVISORY_ONLY",
    }
    await db[LLM_EVAL_RUNS].insert_one(dict(doc))
    return {"run_id": run_id, "summary": summary, "n": len(rows)}


def _jaccard_tokens(a: str, b: str) -> float:
    """Crude token-overlap agreement. Swap for embedding cosine
    once the embedding adapter exists."""
    ta = set(_tokenize(a))
    tb = set(_tokenize(b))
    if not ta and not tb:
        return 1.0
    if not ta or not tb:
        return 0.0
    return round(len(ta & tb) / len(ta | tb), 4)


def _tokenize(s: str) -> List[str]:
    return [t for t in re.findall(r"[a-z0-9]+", (s or "").lower()) if t]


def _summarize(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not rows:
        return {"avg_agreement": None, "candidate_ok_rate": None,
                "primary_ok_rate": None}
    n = len(rows)
    avg = sum(r["agreement"] for r in rows) / n
    cand_ok = sum(1 for r in rows if r["candidate"]["ok"]) / n
    prim_ok = sum(1 for r in rows if r["primary"]["ok"]) / n
    return {
        "avg_agreement": round(avg, 4),
        "candidate_ok_rate": round(cand_ok, 4),
        "primary_ok_rate": round(prim_ok, 4),
    }
