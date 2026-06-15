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
                    # 2026-02-19: include the new auto_submit_skipped
                    # rows so we can distinguish "Shelly correctly
                    # skipped this (HOLD, low-conf, etc.)" from a
                    # genuinely-stuck "Never submitted" intent.
                    "auto_submit_skipped", "auto_submit_failed",
                    # 2026-02-19 (later same day): also include the
                    # auto_router_* kinds. The auto_router is the
                    # background loop that submits intents in
                    # parallel to the dry-run→Shelly chain. Before
                    # this addition the classifier saw NONE of its
                    # rows and bucketed every gate-blocked auto-router
                    # row as "Never submitted (no audit row)", which
                    # is exactly the 2965-ghost mystery in production.
                    "auto_router_passed", "auto_router_blocked",
                    "auto_router_no_trade", "auto_router_error",
                    "auto_router_advisory_only",
                ]},
            },
            {"_id": 0, "intent_id": 1, "kind": 1, "gates": 1, "reason": 1,
             "error": 1, "ts": 1, "skip_category": 1, "classification": 1},
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
    # New stage `shelly_eligible` (2026-02-19): intents that passed
    # dry-run AND were not auto-skipped by Shelly. This is what the
    # operator actually wants to track — the funnel drop between
    # "passed dry-run" and "actually submitted" was misleading
    # because 99% of dry-run-passed intents are HOLD signals Shelly
    # correctly filters.
    funnel = {
        "emitted": 0, "dry_run_passed": 0, "shelly_eligible": 0,
        "submitted": 0, "executed": 0,
    }
    auto_skipped_by_category: Counter[str] = Counter()

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
                elif kind == "auto_submit_skipped":
                    # Shelly looked at it and decided not to submit
                    # (HOLD signal, low confidence, wrong lane, etc.).
                    # By design — surface separately so the operator
                    # can distinguish "filtered correctly" from
                    # "pipeline stuck".
                    category = row.get("skip_category") or "other"
                    outcome = f"auto_submit_skipped_{category}"
                    auto_skipped_by_category[category] += 1
                    top_blockers[("auto_submit_skip", category)] += 1
                elif kind == "auto_submit_failed":
                    outcome = "submit_error"
                    # 2026-02-20: prefer the structured `skip_category`
                    # (e.g. `internal_error`) over the raw exception
                    # string. Groups all chain-raised failures into one
                    # actionable bucket the operator can chase.
                    cat = row.get("skip_category")
                    if cat:
                        top_blockers[("auto_submit_fail", cat)] += 1
                        auto_skipped_by_category[f"failed_{cat}"] += 1
                    else:
                        reason = (row.get("reason") or "auto_submit raised")[:120]
                        top_blockers[("broker", reason)] += 1
                # ─── auto_router_* kinds (2026-02-19, late fix) ────
                # The auto_router is a parallel background submission
                # path. Its audit rows were previously invisible to
                # this classifier — producing the "2965 ghost intents
                # in Never submitted" mystery on production.
                elif kind == "auto_router_passed":
                    outcome = "executed"
                    funnel["submitted"] += 1
                    funnel["executed"] += 1
                    if len(executed_samples) < 10:
                        executed_samples.append(it.get("intent_id"))
                elif kind == "auto_router_blocked":
                    outcome = "gate_chain_blocked"
                    gates = row.get("gates") or []
                    blocker = next(
                        (g for g in gates if not g.get("passed")), None,
                    )
                    label = (blocker.get("name") if blocker
                             else (row.get("reason", "?") or "?"))[:80]
                    top_blockers[("auto_router_gate", label)] += 1
                elif kind == "auto_router_no_trade":
                    outcome = "broker_router_blocked"
                    reason = (row.get("reason") or "broker_router unspecified")[:120]
                    top_blockers[("auto_router_broker", reason)] += 1
                elif kind == "auto_router_error":
                    outcome = "submit_error"
                    reason = (row.get("reason") or row.get("error") or "auto_router error")[:120]
                    top_blockers[("auto_router_broker", reason)] += 1
                elif kind == "auto_router_advisory_only":
                    # Brain emitted HOLD / opinion-only / below-floor —
                    # legitimately not an executable candidate. Mirror
                    # the auto_submit_skipped surfacing so the operator
                    # sees these as "filtered correctly" not "stuck".
                    cls = row.get("classification") or {}
                    cat = (cls.get("reason") or "advisory").replace(" ", "_")
                    outcome = f"advisory_only_{cat}"
                    auto_skipped_by_category[f"advisory_{cat}"] += 1
                    top_blockers[("auto_router_advisory", cat)] += 1
                else:
                    outcome = "never_submitted"

        outcome_counts[outcome] += 1
        by_lane.setdefault(lane, Counter())[outcome] += 1
        by_brain[brain][outcome] += 1

    # 4) Funnel narrative — biggest stage-to-stage drop.
    # `shelly_eligible` (2026-02-19) is computed post-loop:
    #   dry_run_passed − (intents Shelly correctly auto-skipped) =
    #   the count of intents that SHOULD have reached the broker.
    # The drop between shelly_eligible and submitted is the real
    # operator pain point; drops between dry_run_passed and
    # shelly_eligible are HOLD signals, low-conf, etc. — by design.
    total_auto_skipped = sum(auto_skipped_by_category.values())
    funnel["shelly_eligible"] = max(0, funnel["dry_run_passed"] - total_auto_skipped)

    biggest_drop = None
    stages = ["emitted", "dry_run_passed", "shelly_eligible", "submitted", "executed"]
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
        "auto_skipped_by_category": dict(auto_skipped_by_category),
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
