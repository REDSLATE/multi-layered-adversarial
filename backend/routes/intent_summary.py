"""Per-brain intent summary — operator situational awareness.

Doctrine pin (2026-06-10, P2):
The operator needs a one-shot answer to "what has Camaro been
doing for the last hour?" without opening Mongo. This endpoint
aggregates `shared_intents` for one brain into action/lane/verdict
counts plus the last 10 emissions.

Reads `shared_intents` directly — same source the auto-router
consumes — so the summary is always consistent with what the
runtime is acting on.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query

from auth import get_current_user
from db import db
from namespaces import SHARED_INTENTS


router = APIRouter(prefix="/admin/runtime", tags=["admin-runtime"])


@router.get("/{brain}/intent-summary")
async def intent_summary(
    brain: str,
    minutes: int = Query(
        60, ge=1, le=24 * 60,
        description="Trailing window in minutes (default 60, max 1440=24h)",
    ),
    limit: int = Query(
        10, ge=1, le=100,
        description="Number of most-recent intents to include verbatim",
    ),
    _user: dict = Depends(get_current_user),
):
    """Aggregate one brain's recent intent activity.

    Response shape:
        {
            "brain": "camaro",
            "window_minutes": 60,
            "total_intents": 47,
            "by_action": {"BUY": 12, "SELL": 5, "HOLD": 30},
            "by_lane": {"equity": 35, "crypto": 12},
            "by_verdict": {"would_pass": 4, "would_block": 8, "pending": 35},
            "by_symbol": [{"symbol": "AAPL", "count": 12}, ...],
            "last_emitted_at": "2026-06-10T10:14:38.000Z",
            "recent": [...last `limit` intents, newest first...]
        }
    """
    since = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    since_iso = since.isoformat().replace("+00:00", "Z")
    query = {
        # `stack` is the canonical brain_id field on `shared_intents`.
        "stack": brain.lower(),
        # `ingest_ts` is the canonical insert timestamp on shared_intents
        # (NOT `created_at`). ISO-8601 → lexicographic compare works.
        "ingest_ts": {"$gte": since_iso},
    }
    # Pull only the fields the summary needs — avoid dragging the full
    # snapshot / skill_evidence / personality_block payloads back.
    projection = {
        "_id": 0,
        "intent_id": 1,
        "stack": 1,
        "action": 1,
        "symbol": 1,
        "lane": 1,
        "confidence": 1,
        "gate_state": 1,
        "ingest_ts": 1,
        "executed": 1,
    }
    cur = (
        db[SHARED_INTENTS]
        .find(query, projection)
        .sort("ingest_ts", -1)
        .limit(5000)  # hard cap so a wild window can't OOM
    )
    rows: list[dict] = []
    async for d in cur:
        rows.append(d)

    by_action: Counter = Counter()
    by_lane: Counter = Counter()
    by_verdict: Counter = Counter()
    by_symbol: Counter = Counter()
    last_emitted_at: str | None = rows[0]["ingest_ts"] if rows else None
    for r in rows:
        a = r.get("action") or "UNKNOWN"
        by_action[a] += 1
        lane = r.get("lane") or "UNKNOWN"
        by_lane[lane] += 1
        # `gate_state` is the canonical verdict on this collection.
        # Maps {pending, blocked, passed, executed, ...}.
        verdict = r.get("gate_state") or "pending"
        by_verdict[verdict] += 1
        sym = r.get("symbol") or "UNKNOWN"
        by_symbol[sym] += 1

    # Top-15 symbols by count — keep the payload bounded.
    top_symbols = [
        {"symbol": s, "count": c}
        for s, c in by_symbol.most_common(15)
    ]

    return {
        "brain": brain.lower(),
        "window_minutes": minutes,
        "total_intents": len(rows),
        "by_action": dict(by_action),
        "by_lane": dict(by_lane),
        "by_verdict": dict(by_verdict),
        "by_symbol": top_symbols,
        "last_emitted_at": last_emitted_at,
        "recent": rows[:limit],
    }
