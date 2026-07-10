"""Admin endpoints for counterfactual signals.

    GET /api/admin/counterfactuals/stats
        ?since=<iso>   default: 7 days ago
        ?horizon=5m|15m|1h  default: 15m

    Returns verdict distribution, bps aggregates by lane/action/
    block_reason/brain, and top MISSED_WIN / CORRECT_BLOCK samples.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from auth import get_current_user
from db import db
from namespaces import COUNTERFACTUAL_SIGNALS
from shared.counterfactuals import HORIZONS_SEC, resolve_pending_signals

router = APIRouter(prefix="/admin/counterfactuals",
                   tags=["counterfactual-signals"])


def _iso(dt):
    return dt.isoformat()


@router.get("/stats")
async def counterfactual_stats(
    since: Optional[str] = Query(
        default=None,
        description="ISO-8601 UTC lower bound on created_at. Default: 7 days ago.",
    ),
    horizon: str = Query(
        default="15m", description="Horizon to aggregate: 5m, 15m, or 1h.",
    ),
    top_n: int = Query(default=10, ge=1, le=50),
    _user: dict = Depends(get_current_user),
) -> dict:
    """Verdict + bps rollup across `counterfactual_signals`.

    Interpretation:
        MISSED_WIN     — direction was right, block cost us edge.
                         Candidates for gate-threshold relaxation.
        CORRECT_BLOCK  — direction was wrong, block saved us.
                         Confirms gate value.
        UNDETERMINED   — noise, |bps| < 20.
    """
    if horizon not in HORIZONS_SEC:
        raise HTTPException(
            status_code=400,
            detail=f"horizon must be one of {sorted(HORIZONS_SEC)}",
        )
    since_dt = None
    if since:
        try:
            since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail="since must be ISO-8601 UTC",
            )
    if since_dt is None:
        since_dt = datetime.now(timezone.utc) - timedelta(days=7)
    since_iso = _iso(since_dt)

    q = {
        "created_at": {"$gte": since_iso},
        f"outcomes.{horizon}.verdict": {"$exists": True},
    }
    # Verdict counts.
    verdict_counts: dict[str, int] = {}
    async for row in db[COUNTERFACTUAL_SIGNALS].aggregate([
        {"$match": q},
        {"$group": {
            "_id": f"$outcomes.{horizon}.verdict",
            "n": {"$sum": 1},
            "sum_bps": {"$sum": f"$outcomes.{horizon}.return_bps"},
        }},
    ]):
        verdict_counts[row["_id"] or "UNKNOWN"] = {
            "n": row["n"],
            "sum_bps": round(row.get("sum_bps") or 0.0, 2),
            "avg_bps": round(
                (row.get("sum_bps") or 0.0) / row["n"], 2,
            ) if row["n"] else None,
        }

    async def _group_by(field: str) -> dict:
        out = {}
        async for row in db[COUNTERFACTUAL_SIGNALS].aggregate([
            {"$match": q},
            {"$group": {
                "_id": f"${field}",
                "n": {"$sum": 1},
                "sum_bps": {"$sum": f"$outcomes.{horizon}.return_bps"},
            }},
            {"$sort": {"n": -1}},
            {"$limit": 20},
        ]):
            key = row["_id"] or "unknown"
            out[str(key)] = {
                "n": row["n"],
                "sum_bps": round(row.get("sum_bps") or 0.0, 2),
                "avg_bps": round(
                    (row.get("sum_bps") or 0.0) / row["n"], 2,
                ) if row["n"] else None,
            }
        return out

    by_lane = await _group_by("lane")
    by_action = await _group_by("direction")
    by_block_reason = await _group_by("blocked_reason")
    by_brain = await _group_by("brain")

    # Top rankings.
    top_missed = await db[COUNTERFACTUAL_SIGNALS].find(
        {**q, f"outcomes.{horizon}.verdict": "MISSED_WIN"},
        {"_id": 0, "signal_id": 1, "symbol": 1, "lane": 1,
         "direction": 1, "brain": 1, "blocked_reason": 1,
         "entry_reference_price": 1,
         f"outcomes.{horizon}": 1,
         "features": 1, "created_at": 1},
    ).sort(f"outcomes.{horizon}.return_bps", -1).limit(top_n).to_list(top_n)

    top_dodged = await db[COUNTERFACTUAL_SIGNALS].find(
        {**q, f"outcomes.{horizon}.verdict": "CORRECT_BLOCK"},
        {"_id": 0, "signal_id": 1, "symbol": 1, "lane": 1,
         "direction": 1, "brain": 1, "blocked_reason": 1,
         "entry_reference_price": 1,
         f"outcomes.{horizon}": 1,
         "features": 1, "created_at": 1},
    ).sort(f"outcomes.{horizon}.return_bps", 1).limit(top_n).to_list(top_n)

    total_signals = await db[COUNTERFACTUAL_SIGNALS].count_documents({
        "created_at": {"$gte": since_iso},
    })
    tracking = await db[COUNTERFACTUAL_SIGNALS].count_documents({
        "created_at": {"$gte": since_iso}, "status": "tracking",
    })

    return {
        "ok": True,
        "since": since_iso,
        "horizon": horizon,
        "total_signals": total_signals,
        "tracking": tracking,
        "resolved": total_signals - tracking,
        "verdicts": verdict_counts,
        "by_lane": by_lane,
        "by_action": by_action,
        "by_block_reason": by_block_reason,
        "by_brain": by_brain,
        "top_missed_wins": top_missed,
        "top_correct_blocks": top_dodged,
    }


@router.post("/resolve")
async def counterfactual_resolve_now(
    _user: dict = Depends(get_current_user),
) -> dict:
    """Run one resolver pass on demand (piggybacks the same math the
    broker sweep uses)."""
    counts = await resolve_pending_signals(db)
    return {"ok": True, **counts}
