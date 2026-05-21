"""
Paradox Coordinator v0 — Retrain trigger service.

Doctrine pin (2026-02-XX):
    Retrain triggers (per user v0 spec):
      * distillation_winners >= 50
      * OR eval_runs_since_last_train >= 100
      * OR hours_since_last_train >= 24

    What is retrained:
      Only the self_trained/local ADVISORY head. NOT the live
      execution model (there isn't one yet). NOT the broker logic.

    Output:
      A `paradox_retrain_recommendations` row. The operator (or a
      future trainer service) consumes the row and decides whether
      to actually run a training pass. v0 NEVER auto-promotes.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from db import db
from namespaces import (
    LLM_DISTILLATION_QUEUE,
    LLM_EVAL_RUNS,
    PARADOX_RETRAIN_RECOMMENDATIONS,
)

log = logging.getLogger("risedual.paradox_retrain")


# Per-spec thresholds. Tripwire locks these.
TRIGGER_DISTILLATION_WINNERS = 50
TRIGGER_EVAL_RUNS_SINCE_LAST = 100
TRIGGER_HOURS_SINCE_LAST_TRAIN = 24


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _last_retrain_time() -> Optional[datetime]:
    """Read the most-recent retrain recommendation's `consumed_at`
    (or `created_at` as a fallback) to anchor 'since last train'.
    Returns None if no prior retrain row exists."""
    doc = await db[PARADOX_RETRAIN_RECOMMENDATIONS].find_one(
        {}, sort=[("created_at", -1)],
    )
    if not doc:
        return None
    ts = doc.get("consumed_at") or doc.get("created_at")
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    if isinstance(ts, str):
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


async def _distillation_winners_count() -> int:
    return await db[LLM_DISTILLATION_QUEUE].count_documents({})


async def _eval_runs_since(ts: Optional[datetime]) -> int:
    if ts is None:
        return await db[LLM_EVAL_RUNS].count_documents({})
    return await db[LLM_EVAL_RUNS].count_documents(
        {"created_at": {"$gte": ts.isoformat()}},
    )


async def check_retrain(*, force_recommend: bool = False) -> Dict[str, Any]:
    """Evaluate triggers; persist a recommendation row IFF any
    trigger is met (or force_recommend=True). Returns the
    stats and the recommendation (None if no trigger)."""
    last_ts = await _last_retrain_time()
    winners = await _distillation_winners_count()
    eval_runs = await _eval_runs_since(last_ts)
    hours_since = None
    if last_ts is not None:
        hours_since = (_now() - last_ts).total_seconds() / 3600.0
    elif winners == 0 and eval_runs == 0:
        # Cold start with zero data — don't trigger.
        hours_since = 0.0
    else:
        # Cold start but we have data — treat as "infinitely long"
        # since last train so the time-based trigger fires.
        hours_since = float("inf")

    triggers: List[str] = []
    if winners >= TRIGGER_DISTILLATION_WINNERS:
        triggers.append("distillation_winners_threshold")
    if eval_runs >= TRIGGER_EVAL_RUNS_SINCE_LAST:
        triggers.append("eval_runs_threshold")
    if hours_since is not None and hours_since >= TRIGGER_HOURS_SINCE_LAST_TRAIN:
        triggers.append("hours_since_last_train_threshold")

    stats = {
        "distillation_winners": winners,
        "eval_runs_since_last_train": eval_runs,
        "hours_since_last_train": (
            None if hours_since is None or hours_since == float("inf") else round(hours_since, 2)
        ),
        "last_retrain_at": last_ts.isoformat() if last_ts else None,
    }

    rec = None
    if triggers or force_recommend:
        rec_id = str(uuid.uuid4())
        rec = {
            "rec_id": rec_id,
            "triggers": triggers,
            "stats": stats,
            "recommended_target": "self_trained_advisory_head",
            "created_at": _now(),
            "consumed_at": None,
            "consumed_by": None,
        }
        await db[PARADOX_RETRAIN_RECOMMENDATIONS].insert_one(dict(rec))
        # Serialize datetime for return.
        rec["created_at"] = rec["created_at"].isoformat()

    return {
        "ok": True,
        "triggered": bool(triggers),
        "triggers": triggers,
        "stats": stats,
        "recommendation": rec,
    }
