"""Per-(brain, seat) performance analytics.

Answers the user's design question:
    "How good was Camaro as Executor?"
    "How good was Alpha as Governor?"

For each (brain, seat) tuple it aggregates:
    - position stances posted while in that seat (count + breakdown
      by stance long/short/abstain)
    - position calls landed while in that seat (the executor seat's
      auto/manual advances + reject calls)
    - opinions posted while in that seat
    - calibration receipts (when the brain was also producing model
      output during that seat tenure)
    - tenure-days in the seat (sum of all stints)

This gives the operator a hard answer to "should I rotate Camaro into
Executor again?" rather than guessing from gut feel.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone

from fastapi import APIRouter, Depends

from auth import get_current_user
from db import db
from namespaces import (
    DISCUSSION_PARTICIPANTS,
    ROSTER_AUDIT_LOG,
    SHARED_OPINIONS,
    SHARED_POSITION_AUDIT,
    SHARED_POSITION_STANCES,
)
from shared.roster import ROLES


router = APIRouter(prefix="/admin/roster", tags=["seat-performance"])


def _now() -> datetime:
    return datetime.now(timezone.utc)


@router.get("/seat-performance")
async def seat_performance(_user: dict = Depends(get_current_user)):
    """Per-(brain, seat) activity counts.

    Activity is attributed to the seat the brain *held at the time*,
    not its current seat — that's the whole point of the seat_epoch
    snapshot we stamp on every record. A brain that's never held a seat
    shows zero rows for it (suppressed in the output).
    """
    # ── position stances grouped by (brain, posted_as)
    stance_counts: dict[tuple[str, str], dict] = defaultdict(
        lambda: {
            "stances_total": 0,
            "stances_long": 0,
            "stances_short": 0,
            "stances_abstain": 0,
            "avg_confidence": 0.0,
            "_conf_sum": 0.0,
        }
    )
    async for s in db[SHARED_POSITION_STANCES].find(
        {}, {"_id": 0, "brain": 1, "posted_as": 1, "stance": 1, "confidence": 1},
    ):
        brain = s.get("brain")
        seat = s.get("posted_as")
        if not brain or not seat:
            continue
        key = (brain, seat)
        bucket = stance_counts[key]
        bucket["stances_total"] += 1
        bucket[f"stances_{s.get('stance', 'abstain')}"] = (
            bucket.get(f"stances_{s.get('stance', 'abstain')}", 0) + 1
        )
        try:
            bucket["_conf_sum"] += float(s.get("confidence") or 0.0)
        except (TypeError, ValueError):
            pass
    for bucket in stance_counts.values():
        n = bucket["stances_total"]
        bucket["avg_confidence"] = round(bucket["_conf_sum"] / n, 3) if n else 0.0
        bucket.pop("_conf_sum")

    # ── executor calls landed (auto + manual) grouped by (brain, seat)
    call_counts: dict[tuple[str, str], dict] = defaultdict(
        lambda: {"calls_long": 0, "calls_short": 0, "calls_total": 0,
                 "rejects": 0, "auto_advances": 0}
    )
    async for a in db[SHARED_POSITION_AUDIT].find(
        {"action": {"$in": [
            "executor_call", "executor_call_auto", "reject",
        ]}},
        {"_id": 0, "actor": 1, "action": 1, "payload": 1},
    ):
        payload = a.get("payload") or {}
        action = a["action"]
        if action == "executor_call":
            brain = payload.get("executor")
            seat = "executor"  # by definition
            direction = payload.get("direction")
        elif action == "executor_call_auto":
            brain = payload.get("executor")
            seat = "executor"
            direction = payload.get("direction")
        else:  # reject
            brain = None  # operator-initiated; no brain attribution
            seat = None
            direction = None
        if not brain or not seat:
            continue
        bucket = call_counts[(brain, seat)]
        bucket["calls_total"] += 1
        if direction == "long":
            bucket["calls_long"] += 1
        elif direction == "short":
            bucket["calls_short"] += 1
        if action == "executor_call_auto":
            bucket["auto_advances"] += 1

    # ── opinions grouped by runtime (brain) — we don't currently stamp
    # posted_as on opinions in the shared opinions stream, so we report
    # at brain-level only (the seat dimension here is "any"). When the
    # opinions ingest is updated to stamp seat, this'll roll up cleanly.
    opinion_counts: dict[str, int] = defaultdict(int)
    async for o in db[SHARED_OPINIONS].find({}, {"_id": 0, "runtime": 1}):
        if o.get("runtime"):
            opinion_counts[o["runtime"]] += 1

    # ── tenure-days in seat from the roster swap log
    # The roster_log records each swap; reconstruct each brain's tenure
    # in each seat by walking the log in order.
    tenure_seconds: dict[tuple[str, str], float] = defaultdict(float)
    swap_history = await db[ROSTER_AUDIT_LOG].find(
        {}, {"_id": 0, "ts": 1, "action": 1, "payload": 1},
    ).sort("ts", 1).to_list(5000)

    # Walk: maintain a running "who's in each seat since when" map.
    current_since: dict[str, tuple[str | None, datetime]] = {}
    now = _now()
    for entry in swap_history:
        ts = entry.get("ts")
        payload = entry.get("payload") or {}
        after = payload.get("after") or {}
        try:
            ts_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except Exception:  # noqa: BLE001
            continue
        for role in ROLES:
            occupant = after.get(role)
            prev = current_since.get(role)
            if not prev:
                current_since[role] = (occupant, ts_dt)
                continue
            prev_brain, prev_ts = prev
            if prev_brain != occupant:
                # Close out previous tenure
                if prev_brain:
                    tenure_seconds[(prev_brain, role)] += (ts_dt - prev_ts).total_seconds()
                current_since[role] = (occupant, ts_dt)
    # Close out still-open tenures up to now.
    for role, (brain, since) in current_since.items():
        if brain:
            tenure_seconds[(brain, role)] += (now - since).total_seconds()

    # ── assemble per-(brain, seat) matrix
    matrix: list[dict] = []
    for brain in DISCUSSION_PARTICIPANTS:
        for seat in ROLES:
            key = (brain, seat)
            stances = stance_counts.get(key, {})
            calls = call_counts.get(key, {})
            tenure_s = tenure_seconds.get(key, 0.0)
            if not stances and not calls and tenure_s <= 0:
                continue  # never held this seat — suppress
            matrix.append({
                "brain": brain,
                "seat": seat,
                "tenure_days": round(tenure_s / 86_400, 2),
                "stances_total": stances.get("stances_total", 0),
                "stances_long": stances.get("stances_long", 0),
                "stances_short": stances.get("stances_short", 0),
                "stances_abstain": stances.get("stances_abstain", 0),
                "avg_stance_confidence": stances.get("avg_confidence", 0.0),
                "calls_total": calls.get("calls_total", 0),
                "calls_long": calls.get("calls_long", 0),
                "calls_short": calls.get("calls_short", 0),
                "auto_advances": calls.get("auto_advances", 0),
            })

    return {
        "matrix": matrix,
        "brains": list(DISCUSSION_PARTICIPANTS),
        "seats": list(ROLES),
        "opinion_totals_by_brain": dict(opinion_counts),
        "doctrine": (
            "Performance is measured per (brain, seat) tuple. Identity "
            "does not grant authority; seat policy does. A brain that "
            "shines as Executor may flounder as Governor — that's "
            "exactly what this view exists to surface."
        ),
    }
