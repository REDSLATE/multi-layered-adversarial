"""Intent post-mortem — answers "WHY are we not trading?" in one query.

Operator pain point (2026-02-19): two weeks of patches and prod is
still emitting intents that never become trades. The team needs to
stop guessing and see the actual frequency distribution of failure
modes.

This endpoint reads `shared_intents` joined with `shared_gate_results`
for the last N hours and produces a categorical breakdown:

  * `executed`              — intent ran through every layer and a
                              broker order was placed
  * `gate_chain_blocked`    — `_evaluate_gates` rejected (with the
                              FIRST failing gate name surfaced)
  * `broker_router_blocked` — `BrokerRouteBlocked` from the router
                              (Webull cap evaluator, MC receipt
                              rejected, lane disabled, broker frozen)
  * `submit_timeout`        — broker didn't respond in 20s
  * `submit_error`          — broker raised an exception
  * `never_submitted`       — intent emitted, dry-run passed, but
                              the operator never clicked SUBMIT
                              (most common reason for "no trades":
                              the brains are scoring but the human
                              isn't pulling the trigger)
  * `dry_run_blocked`       — emit time dry-run already refused; the
                              intent never even got to the SUBMIT
                              button

The endpoint also surfaces the TOP 5 gate names + broker reasons
across each bucket so the operator can see "70% of blocks are
roadguard_spread_floor on crypto" or "all the executes are AAL
fractional but ETH/USD never makes it".
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List

from fastapi import APIRouter, Depends

from auth import get_current_user
from db import db
from namespaces import SHARED_INTENTS, SHARED_GATE_RESULTS


router = APIRouter(prefix="/admin/intents", tags=["admin-intents"])


@router.get("/post-mortem")
async def intents_post_mortem(
    hours: int = 24,
    _user: dict = Depends(get_current_user),  # noqa: B008
) -> Dict[str, Any]:
    """Aggregate intent → outcome distribution over the recent
    window. Surfaces the dominant failure mode at a glance.

    Args:
        hours: window depth (default 24, min 1, max 168).

    Returns:
        {
          "window_hours": 24,
          "total_intents": int,
          "by_outcome": {
            "executed": int,
            "broker_router_blocked": int,
            "gate_chain_blocked": int,
            "submit_timeout": int,
            "submit_error": int,
            "dry_run_blocked": int,
            "never_submitted": int,
          },
          "top_blockers": [
            {"category": "broker_router", "reason": "...", "count": N},
            {"category": "gate", "name": "roadguard_spread_floor", "count": N},
            ...
          ],
          "by_lane": { "equity": {...}, "crypto": {...} },
          "by_brain": { "camino": {...}, ... },
          "executed_samples": [intent_id, ...],   # up to 10 for spot-check
          "biggest_funnel_drop": str | null,      # e.g. "98% of intents
                                                  # passed dry-run but
                                                  # 0% were submitted"
        }
    """
    hours = max(1, min(int(hours or 24), 168))
    cutoff_iso = (
        datetime.now(timezone.utc) - timedelta(hours=hours)
    ).isoformat()

    # 1) Pull intents in window — small projection, ingest_ts ordered.
    intents = await db[SHARED_INTENTS].find(
        {"ingest_ts": {"$gte": cutoff_iso}},
        {
            "_id": 0, "intent_id": 1, "stack": 1, "lane": 1, "action": 1,
            "symbol": 1, "ingest_ts": 1, "executed": 1, "gate_state": 1,
            "dry_run_state": 1, "executed_at": 1,
        },
    ).to_list(length=10000)

    # 2) Pull the latest submit-related gate-results row per intent.
    intent_ids = [i["intent_id"] for i in intents]
    rows = []
    if intent_ids:
        rows = await db[SHARED_GATE_RESULTS].find(
            {
                "intent_id": {"$in": intent_ids},
                "kind": {"$in": [
                    "submit_passed", "submit_blocked", "submit_no_trade",
                    "submit_timeout", "submit_error",
                ]},
            },
            {"_id": 0, "intent_id": 1, "kind": 1, "gates": 1, "reason": 1, "error": 1, "ts": 1},
        ).to_list(length=20000)
    # Newest row per intent wins.
    latest_by_intent: Dict[str, dict] = {}
    for r in sorted(rows, key=lambda x: x.get("ts") or ""):
        latest_by_intent[r["intent_id"]] = r

    # 3) Classify each intent.
    outcome_counts: Counter[str] = Counter()
    by_lane: Dict[str, Counter] = {"equity": Counter(), "crypto": Counter()}
    by_brain: Dict[str, Counter] = {}
    top_blockers: Counter[tuple[str, str]] = Counter()
    executed_samples: List[str] = []
    funnel = {"emitted": 0, "dry_run_passed": 0, "submitted": 0, "executed": 0}

    for it in intents:
        funnel["emitted"] += 1
        lane = it.get("lane") or "unknown"
        brain = it.get("stack") or "unknown"
        if brain not in by_brain:
            by_brain[brain] = Counter()

        executed = bool(it.get("executed"))
        dry_run = (it.get("dry_run_state") or "").lower()
        if executed:
            outcome = "executed"
            funnel["dry_run_passed"] += 1
            funnel["submitted"] += 1
            funnel["executed"] += 1
            if len(executed_samples) < 10:
                executed_samples.append(it.get("intent_id"))
        elif dry_run in ("blocked", "dry_run_blocked", "fail", "failed"):
            outcome = "dry_run_blocked"
        else:
            funnel["dry_run_passed"] += 1
            row = latest_by_intent.get(it["intent_id"])
            if not row:
                outcome = "never_submitted"
            else:
                kind = row.get("kind")
                if kind == "submit_passed":
                    # Edge case: gate said pass but executed=false — broker probably failed downstream.
                    outcome = "submit_error"
                elif kind == "submit_blocked":
                    outcome = "gate_chain_blocked"
                    gates = row.get("gates") or []
                    blocker = next(
                        (g for g in gates if not g.get("passed")), None,
                    )
                    if blocker:
                        top_blockers[("gate", blocker.get("name") or "?")] += 1
                elif kind == "submit_no_trade":
                    outcome = "broker_router_blocked"
                    reason = (
                        row.get("reason") or "broker_router unspecified"
                    )[:120]
                    top_blockers[("broker_router", reason)] += 1
                elif kind == "submit_timeout":
                    outcome = "submit_timeout"
                    top_blockers[("broker", "submit_timeout_20s")] += 1
                elif kind == "submit_error":
                    outcome = "submit_error"
                    reason = (row.get("error") or row.get("reason") or "?")[:120]
                    top_blockers[("broker", reason)] += 1
                else:
                    outcome = "never_submitted"

        outcome_counts[outcome] += 1
        by_lane.setdefault(lane, Counter())[outcome] += 1
        by_brain[brain][outcome] += 1

    # 4) Funnel narrative — biggest stage-to-stage drop.
    biggest_drop = None
    stages = ["emitted", "dry_run_passed", "submitted", "executed"]
    worst_pct = -1.0
    for prev, curr in zip(stages, stages[1:]):
        prev_n = funnel[prev]
        curr_n = funnel[curr]
        if prev_n == 0:
            continue
        drop_pct = 100.0 * (prev_n - curr_n) / prev_n
        if drop_pct > worst_pct:
            worst_pct = drop_pct
            biggest_drop = (
                f"{drop_pct:.0f}% of intents drop between "
                f"{prev.replace('_', ' ')} ({prev_n}) and "
                f"{curr.replace('_', ' ')} ({curr_n})"
            )

    return {
        "window_hours": hours,
        "total_intents": len(intents),
        "by_outcome": dict(outcome_counts),
        "top_blockers": [
            {"category": cat, "name": name, "count": n}
            for (cat, name), n in top_blockers.most_common(10)
        ],
        "by_lane": {k: dict(v) for k, v in by_lane.items() if v},
        "by_brain": {k: dict(v) for k, v in by_brain.items() if v},
        "executed_samples": executed_samples,
        "funnel": funnel,
        "biggest_funnel_drop": biggest_drop,
    }
