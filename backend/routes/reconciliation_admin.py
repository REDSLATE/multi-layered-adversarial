"""Broker-fill reconciliation admin (2026-06 operator directive).

If the broker says a real fill occurred, Mission Control must show
either the resulting completed outcome or an explicit unresolved
reconciliation exception. Never a silent disappearance.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from auth import get_current_user
from db import db

router = APIRouter(prefix="/admin/reconciliation", tags=["reconciliation"])


@router.get("")
async def reconciliation_status(_user: dict = Depends(get_current_user)):  # noqa: B008
    from shared.reconciliation import (  # noqa: WPS433
        LEDGER, OUTCOMES, STATE_FLAG, reconciliation_counters,
    )
    counters = await reconciliation_counters()
    state = await db["runtime_flags"].find_one(
        {"_id": STATE_FLAG}, {"_id": 0, "last_run": 1}) or {}
    unresolved = await db[LEDGER].find(
        {"link.status": {"$in": ["unlinked", "retry", "unmatched_internal"]}},
        {"symbol": 1, "side": 1, "qty": 1, "price": 1, "ts": 1, "broker": 1,
         "order_id": 1, "link": 1},
    ).sort("ts", -1).max_time_ms(5000).to_list(20)
    recent = await db[OUTCOMES].find(
        {}, {"symbol": 1, "lane": 1, "qty": 1, "entry_avg_price": 1,
             "exit_price": 1, "realized_pnl_usd": 1, "net_return_pct": 1,
             "fees_usd": 1, "fee_pct": 1, "holding_s": 1, "brain": 1,
             "stack": 1, "intent_id": 1, "epoch_id": 1, "exit_ts": 1,
             "measured_cost_eligible": 1, "round_trip_cost_pct": 1},
    ).sort("exit_ts", -1).max_time_ms(5000).to_list(10)
    return {"ok": True, "counters": counters,
            "last_run": state.get("last_run"),
            "unresolved": unresolved, "recent_outcomes": recent}


class RunBody(BaseModel):
    full: bool = False


@router.post("/run")
async def reconciliation_run(
    body: RunBody,
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    """Run now. full=true = historical backfill (walks the entire
    broker trade history) and returns the backfill report."""
    from shared.reconciliation import run_reconciliation  # noqa: WPS433
    report = await run_reconciliation(full=body.full)
    return {"ok": True, "report": report}


@router.get("/repeat-intents")
async def repeat_intents(
    days: float = 7,
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    """Evidence for the multi-brain repeat hypothesis: how many
    observations were suppressed as repeats (post-dedup) and which
    stacks/symbols re-emit the same signal."""
    from datetime import datetime, timedelta, timezone  # noqa: WPS433
    cut = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    agg = await db["missed_entry_outcomes"].aggregate([
        {"$match": {"blocked_at": {"$gte": cut},
                    "repeat_count": {"$gt": 0}}},
        {"$group": {"_id": "$symbol",
                    "observations": {"$sum": 1},
                    "repeats_suppressed": {"$sum": "$repeat_count"},
                    "stacks": {"$addToSet": "$stack"},
                    "repeat_stacks": {"$addToSet": "$repeat_stacks"}}},
        {"$sort": {"repeats_suppressed": -1}}, {"$limit": 12},
    ], maxTimeMS=8000).to_list(12)
    total_obs = await db["missed_entry_outcomes"].count_documents(
        {"blocked_at": {"$gte": cut}})
    total_suppressed = 0
    for a in agg:
        total_suppressed += a.get("repeats_suppressed") or 0
        a["repeat_stacks"] = sorted({s for grp in (a.get("repeat_stacks")
                                                   or []) for s in (grp or [])})
    return {"ok": True, "window_days": days,
            "independent_observations": total_obs,
            "repeats_suppressed": total_suppressed,
            "by_symbol": agg,
            "note": ("repeats are same-symbol signals within the dedup "
                     "window — recorded as repeat_count on the ONE "
                     "independent observation instead of new rows")}
