"""
Distillation queue — successful (prompt, response, outcome) triples
queued for future training of the self-trained model.

Doctrine pin:
    Only `score ≥ +1` preferences are enqueued. The queue is the
    training corpus for `self_trained`. Each row is immutable;
    once a trainer consumes it, the row gets `consumed_at` stamped
    but is NEVER deleted (audit trail of what was learned from).

Collection: `llm_distillation_queue`.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from db import db
from namespaces import (
    LLM_CALLS,
    LLM_DISTILLATION_QUEUE,
    LLM_PREFERENCE_LOG,
)

logger = logging.getLogger("risedual.llm_kernel.distillation")

MIN_ENQUEUE_SCORE = 1


async def enqueue_training_pair(
    *,
    call_id: str,
    score: int,
    outcome: str,
    note: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Pull the full (prompt, response) for `call_id` and enqueue
    it. Returns None if the call isn't found (e.g. ledger row was
    never written) or if the score is below the enqueue threshold.
    Idempotent: re-enqueuing the same call_id is a no-op.
    """
    if score < MIN_ENQUEUE_SCORE:
        return None
    call_doc = await db[LLM_CALLS].find_one({"call_id": call_id}, {"_id": 0})
    if not call_doc:
        logger.warning("enqueue: call_id %s not found in %s", call_id, LLM_CALLS)
        return None
    # Idempotency: bail if already queued.
    existing = await db[LLM_DISTILLATION_QUEUE].find_one(
        {"call_id": call_id}, {"_id": 1},
    )
    if existing:
        return None
    doc = {
        "call_id": call_id,
        "role": call_doc.get("role"),
        "task": call_doc.get("task"),
        "provider": call_doc.get("provider"),
        "model": call_doc.get("model"),
        "prompt": call_doc.get("prompt"),
        "response": call_doc.get("response"),
        "outcome": outcome,
        "score": int(score),
        "note": note or None,
        "consumed_at": None,
        "consumed_by": None,
        "enqueued_at": datetime.now(timezone.utc).isoformat(),
    }
    await db[LLM_DISTILLATION_QUEUE].insert_one(dict(doc))
    return doc


async def dequeue_training_pairs(
    *,
    limit: int = 100,
    min_score: int = MIN_ENQUEUE_SCORE,
    consumer: str = "trainer",
) -> List[Dict[str, Any]]:
    """Pull up to `limit` unconsumed pairs and atomically mark them
    consumed. Trainer processes own these rows from this call.
    """
    out: List[Dict[str, Any]] = []
    now = datetime.now(timezone.utc).isoformat()
    cursor = (
        db[LLM_DISTILLATION_QUEUE]
        .find(
            {"consumed_at": None, "score": {"$gte": min_score}},
            {"_id": 0},
        )
        .sort("enqueued_at", 1)
        .limit(limit)
    )
    async for doc in cursor:
        await db[LLM_DISTILLATION_QUEUE].update_one(
            {"call_id": doc["call_id"], "consumed_at": None},
            {"$set": {"consumed_at": now, "consumed_by": consumer}},
        )
        out.append(doc)
    return out


async def auto_enqueue_recent_winners(window_hours: int = 24) -> Dict[str, Any]:
    """Sweep recent positive preferences and enqueue their calls.
    Useful for a periodic background job (later)."""
    since = _isoformat_hours_ago(window_hours)
    cursor = db[LLM_PREFERENCE_LOG].find(
        {"created_at": {"$gte": since}, "score": {"$gte": MIN_ENQUEUE_SCORE}},
        {"_id": 0},
    )
    enqueued = 0
    skipped = 0
    async for pref in cursor:
        result = await enqueue_training_pair(
            call_id=pref["call_id"],
            score=pref["score"],
            outcome=pref.get("outcome", ""),
            note=pref.get("note"),
        )
        if result:
            enqueued += 1
        else:
            skipped += 1
    return {"window_hours": window_hours, "enqueued": enqueued, "skipped": skipped}


def _isoformat_hours_ago(hours: int) -> str:
    from datetime import timedelta
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
