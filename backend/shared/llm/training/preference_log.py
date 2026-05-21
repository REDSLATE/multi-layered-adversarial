"""
Preference log — brain post-hoc grades on LLM answers.

Doctrine pin:
    Every brain call lands in `llm_calls` (the kernel ledger). Some
    time later — after the decision the LLM advised on actually
    plays out — a brain returns and grades the answer:
      score: int  ∈ [-2, -1, 0, +1, +2]
      outcome: str — free-form  ("trade_won", "trade_lost",
                                "advice_ignored", "advice_helpful")
      note: str   — optional commentary

    Successful preferences (score ≥ +1) feed `distillation_queue`.
    Negative preferences feed eval_harness so we can detect when a
    provider is drifting.

Collection: `llm_preference_log` (one row per call_id, append-only).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from db import db
from namespaces import LLM_PREFERENCE_LOG

logger = logging.getLogger("risedual.llm_kernel.preference")

VALID_SCORES = {-2, -1, 0, 1, 2}


async def record_preference(
    *,
    call_id: str,
    score: int,
    outcome: str,
    note: Optional[str] = None,
    grader: str = "brain",
) -> Dict[str, Any]:
    """Append a preference for a previously-logged LLM call.

    Multiple preferences for the same call_id are allowed — the
    same call can be re-graded as more outcome data arrives. Each
    is its own row.
    """
    if score not in VALID_SCORES:
        raise ValueError(f"score {score!r} not in {sorted(VALID_SCORES)}")
    if not call_id:
        raise ValueError("call_id required")
    if not outcome:
        raise ValueError("outcome required")

    doc = {
        "call_id": call_id,
        "score": int(score),
        "outcome": outcome,
        "note": (note or "").strip()[:1000] or None,
        "grader": grader,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    await db[LLM_PREFERENCE_LOG].insert_one(dict(doc))
    return doc


async def tally_preferences(
    *,
    window_hours: int = 24,
    provider: Optional[str] = None,
) -> Dict[str, Any]:
    """Aggregate scores over a recent window. Optionally filter by
    provider — useful for "how is local doing vs anthropic over the
    last day?"
    """
    from namespaces import LLM_CALLS  # local import to keep the
    # graph clean; preference_log is the producer side, the call
    # ledger is the join side.

    pipeline: List[Dict[str, Any]] = []
    since_iso = _isoformat_hours_ago(window_hours)
    pipeline.append({"$match": {"created_at": {"$gte": since_iso}}})
    pipeline.append({
        "$lookup": {
            "from": LLM_CALLS,
            "localField": "call_id",
            "foreignField": "call_id",
            "as": "_call",
        },
    })
    pipeline.append({"$unwind": "$_call"})
    if provider:
        pipeline.append({"$match": {"_call.provider": provider}})
    pipeline.append({
        "$group": {
            "_id": {"provider": "$_call.provider", "role": "$_call.role"},
            "n": {"$sum": 1},
            "avg_score": {"$avg": "$score"},
            "wins": {
                "$sum": {"$cond": [{"$gte": ["$score", 1]}, 1, 0]},
            },
            "losses": {
                "$sum": {"$cond": [{"$lte": ["$score", -1]}, 1, 0]},
            },
        },
    })

    out = []
    async for row in db[LLM_PREFERENCE_LOG].aggregate(pipeline):
        key = row.pop("_id", {})
        out.append({
            "provider": key.get("provider"),
            "role": key.get("role"),
            **row,
        })
    return {"window_hours": window_hours, "rows": out}


def _isoformat_hours_ago(hours: int) -> str:
    from datetime import timedelta
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
