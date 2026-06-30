"""Era Comparison — "what did the system look like on date X vs today?"

Doctrine pin (2026-02-26, operator-driven): The operator remembers
the system was "trading up a storm" on Friday 2026-02-13, before
the Sunday Feb 15 council/personality refactor landed and the
Monday Feb 16 meltdown ensued. Code from that era is NOT in git
(repo's first commit is 2026-05-07), but the BEHAVIOR is preserved
in the prod Mongo collections: every intent, every emission, every
gate outcome is still there.

This endpoint lets the operator query "what were the brains doing
on 2026-02-13?" and compare it side-by-side with "what are they
doing today?" The diff tells us exactly which knobs to twist to
restore the trading-up-a-storm behavior.

Read-only. Single endpoint:

  GET /api/admin/era-comparison?target_date=2026-02-13&baseline_date=2026-06-29

If `baseline_date` is omitted it defaults to today (UTC).

Per-day stats reported:
  * total intents emitted
  * action distribution (BUY/SELL/HOLD/SHORT/COVER)
  * mean + median confidence
  * % that reached `executed=True`
  * per-brain (stack_canonical) breakdown
  * per-lane breakdown
  * gate_state distribution (where intents died)
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from auth import get_current_user
from db import db


logger = logging.getLogger("risedual.era_comparison")
router = APIRouter(prefix="/admin/era-comparison", tags=["era-comparison"])


def _parse_iso_date(s: str) -> date:
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"date must be YYYY-MM-DD, got {s!r}",
        ) from exc


def _day_window(d: date) -> tuple[str, str]:
    """ISO bounds [start, end) for the 24h UTC day containing `d`.
    `ingest_ts` is stored as an ISO string with tz suffix, so string
    compare works correctly when both sides have the same suffix
    format (which our writers always use)."""
    start = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    return start.isoformat(), end.isoformat()


async def _stats_for_day(d: date) -> dict[str, Any]:
    """Aggregate one day's `shared_intents` snapshot. Single call,
    bounded by maxTimeMS so a missing index can't hang the endpoint."""
    start_iso, end_iso = _day_window(d)
    match = {"ingest_ts": {"$gte": start_iso, "$lt": end_iso}}
    pipeline = [
        {"$match": match},
        {
            "$facet": {
                "total": [{"$count": "n"}],
                "by_action": [
                    {"$group": {"_id": "$action", "n": {"$sum": 1}}},
                    {"$sort": {"n": -1}},
                ],
                "by_lane": [
                    {"$group": {"_id": "$lane", "n": {"$sum": 1}}},
                    {"$sort": {"n": -1}},
                ],
                "by_brain": [
                    {
                        "$group": {
                            "_id": "$stack_canonical",
                            "n": {"$sum": 1},
                            "buy_sell": {
                                "$sum": {
                                    "$cond": [
                                        {"$in": ["$action", ["BUY", "SELL", "SHORT", "COVER"]]},
                                        1, 0,
                                    ]
                                }
                            },
                            "executed": {
                                "$sum": {"$cond": [{"$eq": ["$executed", True]}, 1, 0]}
                            },
                            "mean_conf": {"$avg": "$confidence"},
                        }
                    },
                    {"$sort": {"n": -1}},
                ],
                "by_gate_state": [
                    {"$group": {"_id": "$gate_state", "n": {"$sum": 1}}},
                    {"$sort": {"n": -1}},
                ],
                "executed_count": [
                    {"$match": {"executed": True}},
                    {"$count": "n"},
                ],
                "confidence": [
                    {
                        "$group": {
                            "_id": None,
                            "mean": {"$avg": "$confidence"},
                            "max": {"$max": "$confidence"},
                            "min": {"$min": "$confidence"},
                        }
                    }
                ],
                # Sample 5 random executed intents for a "what fired" snapshot.
                "executed_sample": [
                    {"$match": {"executed": True}},
                    {"$sample": {"size": 5}},
                    {"$project": {
                        "_id": 0, "intent_id": 1, "stack_canonical": 1,
                        "symbol": 1, "action": 1, "confidence": 1,
                        "ingest_ts": 1, "executed_at": 1,
                    }},
                ],
            }
        },
    ]
    try:
        cursor = db.shared_intents.aggregate(
            pipeline,
            maxTimeMS=15_000,
            allowDiskUse=True,
        )
        rows = await cursor.to_list(length=1)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "era-comparison aggregation failed for %s: %s", d, exc,
        )
        return {
            "date": d.isoformat(),
            "error": f"{type(exc).__name__}: {str(exc)[:200]}",
        }

    if not rows:
        return {"date": d.isoformat(), "total": 0, "note": "no intents"}

    r = rows[0]
    total = (r["total"][0]["n"] if r.get("total") else 0)
    executed = (r["executed_count"][0]["n"] if r.get("executed_count") else 0)
    conf = r["confidence"][0] if r.get("confidence") else {}
    return {
        "date": d.isoformat(),
        "total_intents": total,
        "executed": executed,
        "execution_rate_pct": (round(100.0 * executed / total, 2) if total else 0.0),
        "confidence_mean": round(conf.get("mean") or 0, 4),
        "confidence_min": round(conf.get("min") or 0, 4),
        "confidence_max": round(conf.get("max") or 0, 4),
        "by_action": [
            {"action": (a["_id"] or "?"), "n": a["n"],
             "pct": round(100.0 * a["n"] / total, 2) if total else 0.0}
            for a in (r.get("by_action") or [])
        ],
        "by_lane": [
            {"lane": (a["_id"] or "?"), "n": a["n"]}
            for a in (r.get("by_lane") or [])
        ],
        "by_brain": [
            {
                "brain": (a["_id"] or "?"),
                "n": a["n"],
                "buy_sell": a.get("buy_sell", 0),
                "executed": a.get("executed", 0),
                "mean_conf": round(a.get("mean_conf") or 0, 4),
            }
            for a in (r.get("by_brain") or [])
        ],
        "by_gate_state": [
            {"gate_state": (a["_id"] or "?"), "n": a["n"],
             "pct": round(100.0 * a["n"] / total, 2) if total else 0.0}
            for a in (r.get("by_gate_state") or [])
        ],
        "executed_sample": (r.get("executed_sample") or []),
    }


def _diff(target: dict, baseline: dict) -> dict[str, Any]:
    """Compact diff summarizing what changed between the two days.
    Surfaces the deltas an operator actually cares about: execution
    rate, mean confidence, action mix, brain participation."""
    def _g(d: dict, k: str, default=0):
        return d.get(k, default) or 0

    t_total = _g(target, "total_intents")
    b_total = _g(baseline, "total_intents")
    return {
        "intents_delta": b_total - t_total,
        "intents_delta_pct": (
            round(100.0 * (b_total - t_total) / t_total, 2) if t_total else None
        ),
        "execution_rate_delta_pct": round(
            _g(baseline, "execution_rate_pct") - _g(target, "execution_rate_pct"), 2,
        ),
        "confidence_mean_delta": round(
            _g(baseline, "confidence_mean") - _g(target, "confidence_mean"), 4,
        ),
        "summary": (
            f"target {target.get('date')}: {t_total} intents, "
            f"{_g(target, 'execution_rate_pct'):.2f}% executed, "
            f"mean conf {_g(target, 'confidence_mean'):.3f}\n"
            f"baseline {baseline.get('date')}: {b_total} intents, "
            f"{_g(baseline, 'execution_rate_pct'):.2f}% executed, "
            f"mean conf {_g(baseline, 'confidence_mean'):.3f}"
        ),
    }


@router.get("")
async def era_comparison(
    target_date: str = Query(
        ...,
        description="YYYY-MM-DD — the 'how it was' day (e.g. 2026-02-13)",
        examples=["2026-02-13"],
    ),
    baseline_date: Optional[str] = Query(
        default=None,
        description="YYYY-MM-DD — defaults to today (UTC)",
    ),
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    """Side-by-side snapshot of two days' shared_intents activity.
    Use this to figure out what's different between "trading up a
    storm" Friday and today's stalled state."""
    t = _parse_iso_date(target_date)
    b = (
        _parse_iso_date(baseline_date) if baseline_date
        else datetime.now(timezone.utc).date()
    )
    target_stats = await _stats_for_day(t)
    baseline_stats = await _stats_for_day(b)
    return {
        "target": target_stats,
        "baseline": baseline_stats,
        "diff": _diff(target_stats, baseline_stats),
        "doctrine_note": (
            "If target.total_intents = 0, this date predates the "
            "earliest preserved doc in `shared_intents` (TTL or "
            "collection rotation). Try a later target_date or query "
            "the archived intents collection directly."
        ),
    }
