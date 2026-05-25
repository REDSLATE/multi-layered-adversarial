"""Learning Scoreboard — operator post-redeploy truth check.

After the 2026-05-24 lift of `MAX_HOLD_MINUTES` from 24h → 7 days, the
operator needs a single endpoint that answers:

  1. Are new positions staying open past 24h?  → `open_age_buckets`
  2. Are any closing from TP / SL / trailing-stop instead of
     max_hold_time_guard?                    → `closes_by_reason`
  3. Is scratch% falling?                     → `outcome_mix`
  4. Is BRAIN TRACK RECORD showing resolved
     wins/losses?                              → `outcomes_by_brain`
  5. Are Alpha/Camaro/Chevelle writing memory
     labels again?                             → `memory_labels_by_brain`

One JSON response, no Mongo gymnastics. Polled by the operator console.

This is read-only. It does not gate, route, decide, or train anything.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from fastapi import APIRouter, Depends

from auth import get_current_user
from db import db
from namespaces import (
    SHARED_LIVE_POSITIONS,
    SHARED_MEMORY,
    SHARED_OUTCOMES,
)


router = APIRouter(prefix="/admin/learning", tags=["learning-scoreboard"])


# ─── close-reason taxonomy ────────────────────────────────────────────
# Map raw `actor` strings to a stable scoreboard bucket. We pattern-
# match because the actor string is composite (e.g.
# "operator@risedual.io · max_hold_time_guard").
_REASON_BUCKETS: tuple[tuple[str, str], ...] = (
    ("take_profit",       "take_profit"),
    ("stop_loss",         "stop_loss"),
    ("trailing_stop",     "trailing_stop"),
    ("max_hold_time",     "max_hold_time"),
    ("executor",          "executor_call"),     # explicit executor-call-close
    ("operator",          "operator_manual"),
)


def _bucket_for(actor: str | None) -> str:
    if not actor:
        return "unknown"
    a = actor.lower()
    for needle, bucket in _REASON_BUCKETS:
        if needle in a:
            return bucket
    return "other"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _parse(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except Exception:
        return None


# ─── endpoint ────────────────────────────────────────────────────────


@router.get("/scoreboard")
async def learning_scoreboard(
    window_hours: int = 168,
    _user: dict = Depends(get_current_user),  # noqa: B008
) -> Dict[str, Any]:
    """Single read of learning-loop health.

    `window_hours` controls the close-reason and outcome aggregations.
    Defaults to 168 (1 week) — matches the new max_hold_time setting so
    the scoreboard naturally covers one full hold-cycle of activity.
    """
    now = _now()
    since = now - timedelta(hours=window_hours)
    since_iso = _iso(since)

    # ─── 1. Open positions: age buckets ──────────────────────────────
    # We probe both `shared_live_positions` (the position-monitor
    # collection, populated by the broker-fill lifecycle) and
    # `shared_positions` (the governance position store, populated by
    # operator/executor calls). The newer broker pathway writes to the
    # former; the older governance pathway to the latter. Use whichever
    # has data.
    open_positions: List[dict] = []
    for coll, opened_field in (
        (SHARED_LIVE_POSITIONS, "opened_at"),
        ("shared_positions", "created_at"),
    ):
        cursor = db[coll].find(
            {"state": {"$nin": ["closed", "rejected", "expired"]}},
            {"_id": 0, opened_field: 1, "symbol": 1, "lane": 1, "brain": 1,
             "state": 1, "direction": 1},
        )
        async for p in cursor:
            if opened_field != "opened_at" and opened_field in p:
                p["opened_at"] = p.pop(opened_field)
            open_positions.append(p)
        if open_positions:
            break  # prefer the first collection with data

    age_buckets = {"<24h": 0, "24-72h": 0, "72-168h": 0, ">168h": 0}
    oldest_age_hours: float | None = None
    for p in open_positions:
        opened = _parse(p.get("opened_at"))
        if not opened:
            continue
        age_h = (now - opened).total_seconds() / 3600.0
        if oldest_age_hours is None or age_h > oldest_age_hours:
            oldest_age_hours = age_h
        if age_h < 24:
            age_buckets["<24h"] += 1
        elif age_h < 72:
            age_buckets["24-72h"] += 1
        elif age_h < 168:
            age_buckets["72-168h"] += 1
        else:
            age_buckets[">168h"] += 1

    # ─── 2. Closes by reason (within window) ─────────────────────────
    closes_by_reason: Dict[str, int] = {
        "take_profit": 0,
        "stop_loss": 0,
        "trailing_stop": 0,
        "max_hold_time": 0,
        "executor_call": 0,
        "operator_manual": 0,
        "other": 0,
        "unknown": 0,
    }
    closed_total = 0
    cursor = db[SHARED_LIVE_POSITIONS].find(
        {"state": "closed", "closed_at": {"$gte": since_iso}},
        {"_id": 0, "resolved_by": 1, "closed_at": 1},
    )
    async for p in cursor:
        closed_total += 1
        bucket = _bucket_for(p.get("resolved_by"))
        closes_by_reason[bucket] = closes_by_reason.get(bucket, 0) + 1

    # ─── 3 + 4. Outcomes — overall mix + per-brain (within window) ───
    outcome_mix: Dict[str, int] = {"win": 0, "loss": 0, "scratch": 0,
                                    "stopped_out": 0, "other": 0}
    outcomes_by_brain: Dict[str, Dict[str, int]] = {}
    resolved_total = 0
    cursor = db[SHARED_OUTCOMES].find(
        {"resolved_at": {"$gte": since_iso}},
        {"_id": 0, "outcome": 1, "runtime": 1, "brain": 1, "resolved_at": 1},
    )
    async for o in cursor:
        resolved_total += 1
        label = (o.get("outcome") or "other").lower()
        if label not in outcome_mix:
            label = "other"
        outcome_mix[label] += 1
        brain = (o.get("runtime") or o.get("brain") or "unknown").lower()
        bucket = outcomes_by_brain.setdefault(
            brain, {"win": 0, "loss": 0, "scratch": 0, "stopped_out": 0, "other": 0},
        )
        bucket[label] += 1

    # Win-rate per brain (over resolved trades only)
    for brain, b in outcomes_by_brain.items():
        directional = b["win"] + b["loss"] + b["stopped_out"]
        b["directional_resolved"] = directional
        b["total_resolved"] = sum(b.values()) - directional  # = +scratch+other
        b["win_rate"] = round(b["win"] / directional, 3) if directional else None

    scratch_pct = (
        round(outcome_mix["scratch"] / resolved_total, 3)
        if resolved_total else None
    )

    # ─── 5. Memory labels per brain — last write ─────────────────────
    memory_labels_by_brain: Dict[str, Dict[str, Any]] = {}
    for brain in ("alpha", "camaro", "chevelle", "redeye"):
        total = await db[SHARED_MEMORY].count_documents({"runtime": brain})
        last_doc = await db[SHARED_MEMORY].find_one(
            {"runtime": brain},
            sort=[("timestamp", -1)],
            projection={"_id": 0, "timestamp": 1},
        )
        last_at = last_doc.get("timestamp") if last_doc else None
        last_dt = _parse(last_at)
        silent_hours: float | None = None
        if last_dt:
            silent_hours = round((now - last_dt).total_seconds() / 3600.0, 1)
        memory_labels_by_brain[brain] = {
            "total": total,
            "last_write_at": last_at,
            "silent_hours": silent_hours,
            "silent": (silent_hours or 0) > 24 or last_at is None,
        }

    # Schema health — what fraction of outcome rows have a usable label?
    # Operator pin (2026-05-24): "do not tune confidence again until
    # outcome labels exist." If null_rate is high, the resolver is
    # the blocker, not the brains.
    null_outcome_count = await db[SHARED_OUTCOMES].count_documents(
        {"resolved_at": {"$gte": since_iso}, "outcome": None},
    )
    null_outcome_rate: float | None = None
    if resolved_total:
        null_outcome_rate = round(null_outcome_count / resolved_total, 3)

    return {
        "as_of": _iso(now),
        "window_hours": window_hours,
        "config": {
            "max_hold_minutes": 10080,
            "max_hold_days": 7,
            "exec_confidence_floor": 0.35,
            "observation_confidence_floor": 0.30,
        },
        # ── operator's 5 truth checks, in order ──
        "open_positions": {
            "total": len(open_positions),
            "age_buckets": age_buckets,
            "oldest_open_position_age_hours": (
                round(oldest_age_hours, 1) if oldest_age_hours is not None else None
            ),
            "any_past_24h": age_buckets["24-72h"] + age_buckets["72-168h"]
                            + age_buckets[">168h"] > 0,
        },
        "closes_by_reason": {
            **closes_by_reason,
            "_total": closed_total,
            "_pct_max_hold": (
                round(closes_by_reason["max_hold_time"] / closed_total, 3)
                if closed_total else None
            ),
            "_any_natural_exits": (
                closes_by_reason["take_profit"]
                + closes_by_reason["stop_loss"]
                + closes_by_reason["trailing_stop"]
            ) > 0,
        },
        "outcome_mix": {
            **outcome_mix,
            "_total": resolved_total,
            "_scratch_pct": scratch_pct,
            "_null_outcome_count": null_outcome_count,
            "_null_outcome_rate": null_outcome_rate,
            "_schema_health_warning": (
                "Resolver is not writing outcome labels — every row has "
                "outcome=None. Calibrator cannot grade anything. Fix the "
                "resolver before tuning anything else."
            ) if null_outcome_rate and null_outcome_rate > 0.5 else None,
        },
        "outcomes_by_brain": outcomes_by_brain,
        "memory_labels_by_brain": memory_labels_by_brain,
    }
