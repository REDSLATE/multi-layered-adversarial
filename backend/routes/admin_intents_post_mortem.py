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
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

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
                    # 2026-02-20: complete audit-completeness contract
                    # (see `maybe_auto_submit` docstring). Every call
                    # produces exactly one of:
                    #   auto_submit_skipped   — Shelly filter said no
                    #   auto_submit_failed    — submit raised / path leak
                    #   auto_submit_submitted — handed off to broker
                    #   auto_submit_exception — unmapped exception (re-raised)
                    "auto_submit_submitted", "auto_submit_exception",
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
                    # (e.g. `internal_error`, `execution_path_leak`,
                    # `submit_raised`) over the raw exception string.
                    # Groups all chain-raised failures into actionable
                    # buckets the operator can chase.
                    cat = row.get("skip_category")
                    if cat:
                        top_blockers[("auto_submit_fail", cat)] += 1
                        auto_skipped_by_category[f"failed_{cat}"] += 1
                    else:
                        reason = (row.get("reason") or "auto_submit raised")[:120]
                        top_blockers[("broker", reason)] += 1
                # ─── New audit-completeness kinds (2026-02-20) ─────
                # Every maybe_auto_submit call now writes ONE of:
                # skipped / failed / submitted / exception. The first
                # two existed; the last two close the accounting gap
                # that produced the "2586 ghost intents" leak.
                elif kind == "auto_submit_submitted":
                    # Shelly handed off to the broker. The actual
                    # outcome of the order is in `submit_verdict`
                    # (passed/blocked/no_trade) — fold those into the
                    # existing buckets so success vs gate-block is
                    # clear, while still counting the hand-off itself.
                    verdict = (row.get("submit_verdict") or "").lower()
                    if row.get("executed") or verdict == "passed":
                        outcome = "executed"
                        if len(executed_samples) < 10:
                            executed_samples.append(it.get("intent_id"))
                    elif verdict == "blocked":
                        outcome = "gate_chain_blocked"
                        top_blockers[("auto_submit_handoff", "blocked_at_broker")] += 1
                    elif verdict == "no_trade":
                        outcome = "broker_router_blocked"
                        top_blockers[("auto_submit_handoff", "no_trade_at_broker")] += 1
                    else:
                        # Edge: submitted with unrecognized verdict —
                        # still counts as a real hand-off (not a
                        # ghost), surface it for diagnosis.
                        outcome = "submit_error"
                        top_blockers[("auto_submit_handoff", f"verdict={verdict or 'unknown'}")] += 1
                elif kind == "auto_submit_exception":
                    # Unmapped exception in maybe_auto_submit body.
                    # The function re-raises after writing this row,
                    # so the chain's catch-all may add a second row;
                    # the newest-row-wins aggregator picks one. This
                    # branch ensures the row is operator-visible.
                    outcome = "submit_error"
                    top_blockers[("auto_submit_fail", "exception_in_chain")] += 1
                    auto_skipped_by_category["failed_exception"] += 1
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


# ─── Ghost-intent replay (2026-02-20 operator escape hatch) ─────────
# When the audit-completeness contract was added to maybe_auto_submit
# it only writes rows for FUTURE calls. Intents emitted BEFORE the
# fix remain stuck at "Never submitted (no audit row)" with no way to
# diagnose them through the post-mortem panel. This endpoint replays
# them through the now-instrumented chain so the contract produces
# the missing audit rows. Same gate chain, same Shelly policy, same
# broker caps — no bypass.


@router.post("/replay-ghosts")
async def replay_ghost_intents(
    hours: int = 24,
    limit: int = 500,
    _user: dict = Depends(get_current_user),  # noqa: B008
) -> Dict[str, Any]:
    """Find intents in the last N hours that are executed=false AND
    have no auto_submit_* / auto_router_* / submit_* audit row, then
    re-invoke `maybe_auto_submit(intent_id)` on each. The bulletproof
    contract guarantees one terminal audit row per call, so the next
    post-mortem refresh will surface what's actually blocking them.

    Args:
        hours: scan window (default 24, max 168).
        limit: hard cap on the number of replays this call kicks off
            (default 500). Successive calls drain the rest.

    Safety:
        * Operator-auth required.
        * Replays go through the same execution_submit path real
          intents do — gates run, caps enforced, audit rows written.
        * `limit` defaults to 500 so a 5000-ghost backlog drains in
          successive clicks rather than one tidal wave.
    """
    hours = max(1, min(int(hours or 24), 168))
    limit = max(1, min(int(limit or 500), 2000))
    cutoff_iso = (
        datetime.now(timezone.utc) - timedelta(hours=hours)
    ).isoformat()

    candidates = await db[SHARED_INTENTS].find(
        {
            "ingest_ts": {"$gte": cutoff_iso},
            "executed": {"$ne": True},
            "dry_run_state": {"$nin": ["blocked", "dry_run_blocked", "fail", "failed"]},
        },
        {"_id": 0, "intent_id": 1},
    ).limit(limit * 4).to_list(limit * 4)

    candidate_ids = [c["intent_id"] for c in candidates]
    rows = await db[SHARED_GATE_RESULTS].find(
        {
            "intent_id": {"$in": candidate_ids},
            "kind": {"$in": [
                "submit_passed", "submit_blocked", "submit_no_trade",
                "submit_timeout", "submit_error",
                "auto_submit_skipped", "auto_submit_failed",
                "auto_submit_submitted", "auto_submit_exception",
                "auto_router_passed", "auto_router_blocked",
                "auto_router_no_trade", "auto_router_error",
                "auto_router_advisory_only",
            ]},
        },
        {"_id": 0, "intent_id": 1},
    ).to_list(len(candidate_ids) or 1)
    already_audited = {r["intent_id"] for r in rows}
    ghosts = [c["intent_id"] for c in candidates if c["intent_id"] not in already_audited]
    ghosts = ghosts[:limit]

    from shared.auto_submit_policy import maybe_auto_submit

    by_terminal_kind: Dict[str, int] = {
        "auto_submit_skipped": 0,
        "auto_submit_failed": 0,
        "auto_submit_submitted": 0,
        "auto_submit_exception": 0,
    }
    errors = 0
    replayed = 0
    for intent_id in ghosts:
        try:
            await maybe_auto_submit(intent_id)
            replayed += 1
        except Exception:  # noqa: BLE001
            errors += 1
            replayed += 1

    if ghosts:
        terminals = await db[SHARED_GATE_RESULTS].find(
            {
                "intent_id": {"$in": ghosts},
                "kind": {"$in": list(by_terminal_kind.keys())},
            },
            {"_id": 0, "intent_id": 1, "kind": 1, "ts": 1, "skip_category": 1},
        ).to_list(len(ghosts) * 2)
        latest: Dict[str, str] = {}
        for r in sorted(terminals, key=lambda x: x.get("ts") or ""):
            latest[r["intent_id"]] = r["kind"]
        for kind in latest.values():
            if kind in by_terminal_kind:
                by_terminal_kind[kind] += 1

    return {
        "window_hours": hours,
        "limit": limit,
        "scanned": len(candidates),
        "already_audited": len(already_audited),
        "replayed": replayed,
        "errors": errors,
        "by_terminal_kind": by_terminal_kind,
        "remaining_ghosts_estimate": max(
            0,
            len(candidates) - len(already_audited) - replayed,
        ),
    }



# ─── Single-intent trace (2026-02-20 operator directive) ────────────
# "Show me a single intent that was Shelly-eligible and trace every
# step until it either became a broker order or died."
#
# This endpoint answers that question for ANY intent_id, not just the
# aggregate. It pulls the intent row, every gate-result row keyed to
# it (sorted oldest → newest), and any execution receipt — then
# returns a chronological timeline plus a derived "died_at" verdict.
#
# Read-only. Operator uses this when the post-mortem aggregator
# screams "2586 eligible / 0 submitted" and they need to know which
# gate ate ONE specific intent so the bug can be fixed at the source.


@router.get("/{intent_id}/trace")
async def trace_intent(
    intent_id: str,
    _user: dict = Depends(get_current_user),  # noqa: B008
) -> Dict[str, Any]:
    """Full lifecycle of one intent: emit → gates → submit → broker.

    Returns:
        {
          "intent_id": str,
          "intent": {symbol, lane, action, stack, confidence,
                     dry_run_state, executed, ingest_ts, ...} | null,
          "timeline": [
            {ts, kind, summary, gate_name?, reason?, skip_category?,
             passed?, raw_keys}, ...
          ],
          "receipts": [...],
          "verdict": "executed" | "blocked_at_<gate>" | "skipped_<cat>"
                     | "no_audit_row" | "intent_not_found",
          "summary": str  # one-liner the operator can paste in chat
        }
    """
    from namespaces import EXECUTION_RECEIPTS  # noqa: WPS433

    intent = await db[SHARED_INTENTS].find_one(
        {"intent_id": intent_id}, {"_id": 0},
    )

    rows = await db[SHARED_GATE_RESULTS].find(
        {"intent_id": intent_id},
        {"_id": 0},
    ).to_list(length=500)
    rows.sort(key=lambda r: r.get("ts") or "")

    receipts = await db[EXECUTION_RECEIPTS].find(
        {"intent_id": intent_id},
        {"_id": 0},
    ).to_list(length=20)
    receipts.sort(key=lambda r: r.get("ts") or r.get("created_at") or "")

    timeline: List[Dict[str, Any]] = []
    for r in rows:
        kind = r.get("kind") or "unknown"
        entry: Dict[str, Any] = {
            "ts": r.get("ts"),
            "kind": kind,
            "raw_keys": sorted([k for k in r.keys() if k not in ("intent_id",)]),
        }
        # First failing gate, if this row carries the gate-chain output.
        gates = r.get("gates") or []
        if gates:
            failed = next((g for g in gates if not g.get("passed")), None)
            if failed:
                entry["gate_name"] = failed.get("name")
                entry["gate_reason"] = failed.get("reason") or failed.get("detail")
                entry["passed"] = False
            else:
                entry["passed"] = True
            entry["gate_count"] = len(gates)
        # Reason / skip_category / verdict shortcuts.
        for k in ("reason", "skip_category", "submit_verdict", "tier",
                  "verdict", "broker_order_id", "error"):
            v = r.get(k)
            if v is not None:
                entry[k] = v
        # One-liner summary for the UI table.
        if kind in ("auto_submit_skipped", "auto_router_advisory_only"):
            entry["summary"] = (
                f"{kind} · {entry.get('skip_category', entry.get('reason', '?'))}"
            )
        elif kind in ("submit_blocked", "auto_router_blocked"):
            entry["summary"] = (
                f"{kind} · {entry.get('gate_name', entry.get('reason', '?'))}"
            )
        elif kind in ("auto_submit_submitted",):
            entry["summary"] = (
                f"{kind} · broker_verdict={entry.get('submit_verdict', '?')}"
            )
        elif kind in ("auto_router_passed", "submit_passed"):
            entry["summary"] = (
                f"{kind} · order={entry.get('broker_order_id', '?')}"
            )
        elif kind in ("submit_error", "auto_router_error",
                      "auto_submit_failed", "auto_submit_exception"):
            entry["summary"] = (
                f"{kind} · {entry.get('reason', entry.get('error', '?'))[:160]}"
            )
        else:
            entry["summary"] = kind
        timeline.append(entry)

    # Derived verdict — the operator's "where did it die?" answer.
    if intent is None:
        verdict = "intent_not_found"
        summary_line = f"intent_id={intent_id} has no row in shared_intents"
    elif intent.get("executed"):
        verdict = "executed"
        broker_id = None
        for rcpt in reversed(receipts):
            broker_id = rcpt.get("broker_order_id") or broker_id
            if broker_id:
                break
        summary_line = (
            f"executed · {intent.get('stack')} {intent.get('action')} "
            f"{intent.get('symbol')} · broker_order_id={broker_id or '?'}"
        )
    elif not timeline:
        verdict = "no_audit_row"
        summary_line = (
            f"emitted but ZERO gate_result rows · "
            f"dry_run_state={intent.get('dry_run_state', '?')} · "
            f"this is a ghost intent (chain never reached audit-write)"
        )
    else:
        last = timeline[-1]
        kind = last.get("kind")
        if kind == "auto_submit_skipped":
            verdict = f"skipped_{last.get('skip_category', 'unknown')}"
            summary_line = (
                f"skipped at maybe_auto_submit · "
                f"{last.get('skip_category', '?')} · "
                f"reason={last.get('reason', '?')[:120]}"
            )
        elif kind in ("submit_blocked", "auto_router_blocked"):
            gate = last.get("gate_name", "?")
            verdict = f"blocked_at_{gate}"
            summary_line = (
                f"blocked at gate `{gate}` · "
                f"reason={last.get('gate_reason') or last.get('reason') or '?'}"
            )
        elif kind == "auto_router_advisory_only":
            verdict = "advisory_only"
            summary_line = (
                f"auto_router classified advisory_only · "
                f"reason={last.get('reason', '?')}"
            )
        elif kind == "auto_submit_submitted":
            verdict = f"submitted_verdict_{last.get('submit_verdict', 'unknown')}"
            summary_line = (
                f"handed off to broker · "
                f"submit_verdict={last.get('submit_verdict', '?')} · "
                f"executed={intent.get('executed')}"
            )
        elif kind in ("submit_error", "auto_router_error",
                      "auto_submit_failed", "auto_submit_exception"):
            verdict = "submit_error"
            summary_line = (
                f"chain raised · {kind} · "
                f"reason={last.get('reason') or last.get('error') or '?'}"
            )
        else:
            verdict = f"last_row_{kind}"
            summary_line = f"last audit row was {kind}"

    return {
        "intent_id": intent_id,
        "intent": intent,
        "timeline": timeline,
        "receipts": receipts,
        "verdict": verdict,
        "summary": summary_line,
    }



# ─── Pre-market readiness check (2026-02-20) ────────────────────────
# "Will trading fire when the market opens?" One GET → green/red on
# every master switch + dependency, in one place, so the operator can
# confirm the system is armed BEFORE 9:30 ET rather than discovering
# at end of day it wasn't.


@router.get("/system-readiness")
async def system_readiness(
    _user: dict = Depends(get_current_user),  # noqa: B008
) -> Dict[str, Any]:
    """Aggregate the 5 master switches + broker connectivity + seat
    occupancy into a single green/red board.

    All-green ⇒ when a brain emits a BUY/SELL intent during RTH
    that passes the gate chain, an order goes to the broker.

    Returns:
        {
          "ready_to_trade": bool,
          "checks": [{name, status, detail, fix_endpoint?}, ...],
          "summary": "READY" | "BLOCKED at <first red>",
        }
    """
    from shared.auto_submit_policy import get_policy as _get_shelly
    from shared.auto_router import get_status as _get_router_status
    from shared.lane_execution import get_toggles as _get_lane_toggles
    from routes.trading_controls import get_trading_status as _get_trade_ctrl

    checks: List[Dict[str, Any]] = []

    # 1. trading_controls.enabled (auto-router master gate, fail-closed)
    try:
        tc = await _get_trade_ctrl()
        tc_ok = bool(tc.get("enabled"))
        checks.append({
            "name": "trading_controls",
            "status": "green" if tc_ok else "red",
            "detail": (
                f"runtime={tc_ok} · updated_by={tc.get('updated_by')} · "
                f"reason={tc.get('reason')!r}"
            ),
            "fix_endpoint": (
                "POST /api/admin/trading/toggle {enabled:true, reason:'...'}"
                if not tc_ok else None
            ),
        })
    except Exception as e:  # noqa: BLE001
        checks.append({
            "name": "trading_controls",
            "status": "red",
            "detail": f"check failed: {e}",
        })

    # 2. auto_router task alive
    try:
        rs = _get_router_status()
        alive = bool(rs.get("task_alive"))
        ticks = int(rs.get("tick_count") or 0)
        checks.append({
            "name": "auto_router_loop",
            "status": "green" if (alive and ticks > 0) else "red",
            "detail": (
                f"task_alive={alive} · tick_count={ticks} · "
                f"last_tick_ts={rs.get('last_tick_ts')} · "
                f"last_tick_error={rs.get('last_tick_error')}"
            ),
            "fix_endpoint": (
                "POST /api/admin/auto-router/start" if not alive else None
            ),
        })
    except Exception as e:  # noqa: BLE001
        checks.append({
            "name": "auto_router_loop",
            "status": "red",
            "detail": f"check failed: {e}",
        })

    # 3. Shelly policy enabled
    try:
        sp = _get_shelly()
        sp_ok = bool(sp.get("enabled"))
        checks.append({
            "name": "shelly_auto_submit_policy",
            "status": "green" if sp_ok else "red",
            "detail": (
                f"enabled={sp_ok} · source={sp.get('source')} · "
                f"confidence_min={sp.get('confidence_min')}"
            ),
            "fix_endpoint": (
                "POST /api/admin/auto-submit/policy {enabled:true, "
                "confidence_min:0.65, reason:'...'}"
                if not sp_ok else None
            ),
        })
    except Exception as e:  # noqa: BLE001
        checks.append({
            "name": "shelly_auto_submit_policy",
            "status": "red",
            "detail": f"check failed: {e}",
        })

    # 4 & 5. Lane execution toggles
    try:
        lt = await _get_lane_toggles()
        for lane in ("equity", "crypto"):
            on = bool(lt.get(lane))
            checks.append({
                "name": f"lane_execution_{lane}",
                "status": "green" if on else "red",
                "detail": (
                    f"enabled={on} · updated_by={lt.get(f'{lane}_updated_by')}"
                ),
                "fix_endpoint": (
                    f"POST /api/admin/execution/lane-toggles "
                    f"{{lane:'{lane}', enabled:true, "
                    f"confirm:'I authorize {lane} trading'}}"
                    if not on else None
                ),
            })
    except Exception as e:  # noqa: BLE001
        checks.append({
            "name": "lane_execution_toggles",
            "status": "red",
            "detail": f"check failed: {e}",
        })

    # 6. Seat occupancy — at least one executor seat per lane.
    try:
        from shared.executor_seat import get_seat_holder, seats_with_execute
        for lane in ("equity", "crypto"):
            holder = None
            for seat in seats_with_execute(lane):
                h = await get_seat_holder(seat)
                if h:
                    holder = (seat, h)
                    break
            checks.append({
                "name": f"executor_seat_{lane}",
                "status": "green" if holder else "red",
                "detail": (
                    f"seat={holder[0]} holder={holder[1]}"
                    if holder else "no executor-seat holder for this lane"
                ),
                "fix_endpoint": (
                    "POST /api/admin/roster/assign — seat one of the brains"
                    if not holder else None
                ),
            })
    except Exception as e:  # noqa: BLE001
        checks.append({
            "name": "executor_seats",
            "status": "red",
            "detail": f"check failed: {e}",
        })

    # 7. Market-hours awareness (RTH for equity; crypto trades 24/7).
    # Webull rejects equity orders outside RTH with HTTP 417. Uses the
    # canonical `shared.market_hours` module (DST-aware + holiday-aware)
    # — the same one the auto-submitter gate checks against, so the
    # operator-facing readiness panel and the auto-submit decision
    # agree on whether a market is open.
    now_utc = datetime.now(timezone.utc)
    try:
        from shared.market_hours import (
            is_equity_rth, next_rth_open_iso,
        )
        is_rth = is_equity_rth(now_utc=now_utc)
        next_open = next_rth_open_iso(now_utc=now_utc) if not is_rth else ""
        weekday = now_utc.strftime("%A")
        detail = (
            f"{weekday} {now_utc.isoformat()[:19]}Z · "
            f"RTH={'YES' if is_rth else 'NO (Webull will 417 equity orders)'}"
        )
        if next_open:
            detail += f" · next open {next_open[:19]}Z"
    except Exception as e:  # noqa: BLE001
        is_rth = False
        detail = f"market_hours check failed: {e}"
    checks.append({
        "name": "market_hours_equity",
        "status": "green" if is_rth else "amber",
        "detail": detail,
        # Not a "fix" — just info. Crypto trades 24/7 via Kraken.
    })

    reds = [c for c in checks if c.get("status") == "red"]
    ready = len(reds) == 0
    if ready:
        # Allow "amber" (market closed) to coexist with READY — the
        # system is armed; orders fire when the market opens.
        summary = "READY (orders will fire when an intent qualifies)"
    else:
        first_red = reds[0]
        summary = f"BLOCKED at `{first_red['name']}` — {first_red.get('detail', '')[:120]}"

    return {
        "ready_to_trade": ready,
        "checks": checks,
        "summary": summary,
        "checked_at": now_utc.isoformat(),
    }



# ─── One-button ARM / DISARM (2026-02-20 operator directive) ─────────
# "Could you make one switch that turns them all on?"
#
# Flips the five master gates in one call:
#   1. trading_controls.enabled               (auto-router master)
#   2. runtime_flags.auto_router_enabled      (background loop)
#   3. shared_auto_submit_policy.enabled      (Shelly)
#   4. lane_execution_toggles.equity          (equity routing)
#   5. lane_execution_toggles.crypto          (crypto routing)
#
# Doctrine: this is a CONVENIENCE wrapper. Every individual endpoint
# below still works for fine-grained control. ARM does NOT skip any
# safety: the broker connectivity gate, council math, governor floor,
# RoadGuard stops, market-hours, and per-order caps all still apply
# downstream. ARM only flips the operator's opt-in master switches.
#
# Each individual flip is audit-logged by its own endpoint's writer,
# so the audit trail is complete (per-switch + the ARM call's own
# `system_arm_audit` row that summarizes the batch).


class ArmIn(BaseModel):
    reason: str = Field(..., min_length=4, max_length=400,
                        description="Why are you arming the system?")
    # 2026-02-20: use Optional[float] instead of `float | None` —
    # the PEP 604 union syntax is only evaluated lazily under
    # `from __future__ import annotations`, which this module does
    # not import. Without that, Pydantic resolves annotations at
    # class-creation time, and Python <3.10 raises:
    #   TypeError: unsupported operand type(s) for |: 'type' and 'NoneType'
    # That ImportError takes down the whole route module on prod,
    # which then 5xx's the FastAPI app → Cloudflare returns 520 to
    # the operator. Optional[X] is portable across all versions.
    confidence_min: Optional[float] = Field(
        default=0.65, ge=0.0, le=1.0,
        description="Optional override for Shelly's confidence_min on flip. "
                    "Defaults to 0.65 which lets ~mid-conviction trades through.",
    )


@router.post("/system-arm")
async def system_arm(
    body: ArmIn,
    user: dict = Depends(get_current_user),  # noqa: B008
) -> Dict[str, Any]:
    """Flip all five master switches ON in one call. Each flip is
    independent — if any single switch fails, the others still flip,
    and the response surfaces which ones errored. The final
    readiness snapshot tells the operator whether the system is live.
    """
    from shared.auto_submit_policy import set_policy_async
    from shared.lane_execution import set_lane_toggle
    from shared.auto_router import start_auto_router_if_enabled
    from routes.trading_controls import set_trading_enabled

    actor = (user or {}).get("email") or "operator"
    reason = body.reason.strip()
    flipped: List[Dict[str, Any]] = []

    async def _safe(name: str, coro_or_call):
        try:
            res = await coro_or_call if hasattr(coro_or_call, "__await__") else coro_or_call
            flipped.append({"switch": name, "ok": True, "detail": str(res)[:160] if res else "ok"})
        except Exception as e:  # noqa: BLE001
            flipped.append({"switch": name, "ok": False, "error": f"{type(e).__name__}: {e}"[:240]})

    # 1. trading_controls
    await _safe(
        "trading_controls",
        set_trading_enabled(True, reason, actor),
    )
    # 2. auto_router runtime flag + start
    try:
        await db["runtime_flags"].update_one(
            {"_id": "auto_router_enabled"},
            {"$set": {
                "enabled": True,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "updated_by": actor,
            }},
            upsert=True,
        )
        # `start_auto_router_if_enabled` is sync and idempotent
        # (returns silently if env disabled or task already alive).
        start_auto_router_if_enabled()
        flipped.append({"switch": "auto_router_loop", "ok": True, "detail": "flag + start"})
    except Exception as e:  # noqa: BLE001
        flipped.append({
            "switch": "auto_router_loop", "ok": False,
            "error": f"{type(e).__name__}: {e}"[:240],
        })
    # 3. Shelly policy
    overrides = {}
    if body.confidence_min is not None:
        overrides["confidence_min"] = body.confidence_min
    await _safe(
        "shelly_auto_submit_policy",
        set_policy_async(enabled=True, **overrides),
    )
    # 4 + 5. Lane toggles
    for lane in ("equity", "crypto"):
        await _safe(
            f"lane_execution_{lane}",
            set_lane_toggle(lane, True, actor),
        )

    # Audit row — the batched ARM action itself, in addition to each
    # individual switch's own audit log.
    try:
        await db["system_arm_audit"].insert_one({
            "ts": datetime.now(timezone.utc).isoformat(),
            "actor": actor,
            "reason": reason,
            "switches": flipped,
            "confidence_min": body.confidence_min,
        })
    except Exception:  # noqa: BLE001
        pass  # audit is best-effort; the operator already has per-switch logs

    # Compute final readiness so the operator sees green/red in the
    # same response.
    readiness = await system_readiness(_user=user)  # noqa: SLF001

    return {
        "ok": all(f.get("ok") for f in flipped),
        "actor": actor,
        "reason": reason,
        "switches": flipped,
        "readiness": readiness,
    }


@router.post("/system-disarm")
async def system_disarm(
    body: ArmIn,
    user: dict = Depends(get_current_user),  # noqa: B008
) -> Dict[str, Any]:
    """Flip all five master switches OFF in one call.

    This is the inverse of `/system-arm`. The auto-router task is NOT
    forcibly killed mid-tick — the runtime flag flip is enough; the
    next tick's `is_trading_enabled` check will return False and the
    router will write `no_trade · trading_controls_disabled` rows
    instead of routing. Existing positions are not touched.
    """
    from shared.auto_submit_policy import set_policy_async
    from shared.lane_execution import set_lane_toggle
    from routes.trading_controls import set_trading_enabled

    actor = (user or {}).get("email") or "operator"
    reason = body.reason.strip()
    flipped: List[Dict[str, Any]] = []

    async def _safe(name: str, coro_or_call):
        try:
            res = await coro_or_call if hasattr(coro_or_call, "__await__") else coro_or_call
            flipped.append({"switch": name, "ok": True, "detail": str(res)[:160] if res else "ok"})
        except Exception as e:  # noqa: BLE001
            flipped.append({"switch": name, "ok": False, "error": f"{type(e).__name__}: {e}"[:240]})

    await _safe("trading_controls",
                set_trading_enabled(False, reason, actor))
    try:
        await db["runtime_flags"].update_one(
            {"_id": "auto_router_enabled"},
            {"$set": {
                "enabled": False,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "updated_by": actor,
            }},
            upsert=True,
        )
        flipped.append({"switch": "auto_router_loop", "ok": True, "detail": "flag flipped off"})
    except Exception as e:  # noqa: BLE001
        flipped.append({
            "switch": "auto_router_loop", "ok": False,
            "error": f"{type(e).__name__}: {e}"[:240],
        })
    await _safe("shelly_auto_submit_policy",
                set_policy_async(enabled=False))
    for lane in ("equity", "crypto"):
        await _safe(f"lane_execution_{lane}",
                    set_lane_toggle(lane, False, actor))

    try:
        await db["system_arm_audit"].insert_one({
            "ts": datetime.now(timezone.utc).isoformat(),
            "actor": actor,
            "reason": reason,
            "action": "disarm",
            "switches": flipped,
        })
    except Exception:  # noqa: BLE001
        pass

    readiness = await system_readiness(_user=user)  # noqa: SLF001
    return {
        "ok": all(f.get("ok") for f in flipped),
        "actor": actor,
        "reason": reason,
        "switches": flipped,
        "readiness": readiness,
    }
