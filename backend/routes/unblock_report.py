"""Lane Unblocker Report — "why didn't this lane trade?"

Doctrine pin (2026-02-26, operator-directed):
    "Add this endpoint/tile. It should answer ONE question:
     Why did the last 100 equity/crypto intents not become orders?
     Healthy system != trading system. The system can be healthy
     and still not trade because one downstream rule blocks every
     candidate. Make the system tell you exactly where each
     candidate dies."

This endpoint classifies the LAST N (default 100) intents in each
lane into terminal stages and aggregates the counts so the operator
can see — at a glance — which gate is currently the per-lane
chokepoint.

Future work (NOT in this PR, too risky right after a fragile period):
    Write `intent["execution_terminal_reason"] = {stage, code, msg,
    lane, ts}` from the pipeline. Then this endpoint stops having
    to reverse-engineer the terminal stage from `gate_state` +
    `shared_gate_results` audit rows. For now, best-effort
    classification using the data we already have.

Live checks per lane (operator's priority list 1-5):
    1. router_ticking          — auto_router task alive + tick_count>0
    2. lane_execution_enabled  — operator's master kill switch
    3. broker_lane_enabled     — broker_lane_admin toggle
    4. seat_assigned           — any brain holds an executor seat for the lane
    5. broker_loaded           — adapter loaded with credentials
    6. direct_execute_mode     — fast-path enabled
    7. auto_submit_policy_ok   — tier allows this lane + BUY/SELL

If any of (1-7) is false, the report flags it as a STRUCTURAL
problem — no amount of strategy tuning will help until it's
fixed. THESE are the unblockers.

READ-ONLY. No mutations. Safe to poll.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Query

from auth import get_current_user
from db import db


logger = logging.getLogger("risedual.unblock_report")
router = APIRouter(prefix="/admin/trading", tags=["unblock-report"])


_ROUTABLE_ACTIONS = {"BUY", "SELL", "SHORT", "COVER"}
_TERMINAL_GATE_STATES = {"blocked", "no_trade", "advisory_only"}


# ─── Per-intent terminal-stage classifier ──────────────────────────
async def _classify_intent(intent: dict) -> tuple[str, str]:
    """Returns `(stage, code)` for one intent based on persisted fields
    plus its audit-row trail in `shared_gate_results`.

    Stages (worst → best):
      hold_bias            — action was HOLD; not a routable candidate
      router_never_picked  — candidate, alive, but no audit row at all
                              (auto-router didn't or couldn't pick it up)
      blocked_by_gate      — `gate_state` is terminal (blocked/no_trade/
                              advisory_only) — pipeline rejected it
      blocked_by_router    — direct_execute_blocked: broker route blocked
                              (lane toggle, freeze, cap, broker_connected)
      blocked_by_broker    — direct_execute_failed: broker raised an
                              exception (Webull 417, Kraken auth, etc.)
      action_skipped       — direct_execute_skipped: non-routable action
      submitted            — executed=True; an order was filed
    """
    action = (intent.get("action") or "").upper()
    if action not in _ROUTABLE_ACTIONS:
        return "hold_bias", "ACTION_NOT_ROUTABLE"

    if intent.get("executed"):
        return "submitted", "OK"

    intent_id = intent.get("intent_id")
    if not intent_id:
        return "router_never_picked", "NO_INTENT_ID"

    # Audit-row trail. We only care about direct_execute_* kinds; if
    # the operator is in legacy auto-submit mode there'd be different
    # row kinds, but in the current era direct_execute is the path.
    audit = await db.shared_gate_results.find(
        {"intent_id": intent_id, "kind": {"$in": [
            "direct_execute_submitted", "direct_execute_blocked",
            "direct_execute_failed",    "direct_execute_skipped",
        ]}},
        {"_id": 0, "kind": 1, "reason": 1, "exception_message": 1,
         "exception_type": 1, "skip_category": 1},
    ).sort("ts", -1).max_time_ms(2000).to_list(1)

    if audit:
        row = audit[0]
        kind = row.get("kind", "")
        if kind == "direct_execute_submitted":
            return "submitted", "OK"
        if kind == "direct_execute_blocked":
            return "blocked_by_router", (row.get("reason") or "BROKER_ROUTE_BLOCKED")[:80]
        if kind == "direct_execute_failed":
            return "blocked_by_broker", (
                f"{row.get('exception_type','?')}: {(row.get('exception_message') or '')[:60]}"
            )
        if kind == "direct_execute_skipped":
            return "action_skipped", (row.get("skip_category") or "SKIPPED")[:80]

    # No audit row.
    gate_state = (intent.get("gate_state") or "").lower()
    if gate_state in _TERMINAL_GATE_STATES:
        # Pipeline blocked it before direct-execute saw it (e.g. legacy
        # gate chain ran, or direct-execute was disabled when this intent
        # was emitted).
        return "blocked_by_gate", gate_state.upper()

    # Candidate, alive, no audit row, gate_state not terminal — auto-
    # router didn't pick it up yet OR can't pick it up. THIS is the
    # silent-bottleneck case the operator most cares about.
    return "router_never_picked", "NO_AUDIT_ROW"


# ─── Live structural checks ────────────────────────────────────────
async def _live_checks_for_lane(lane: str) -> dict[str, Any]:
    """Probe the seven operator-priority blockers for one lane.
    Returns one bool per check + a `structural_blockers` list of
    check names that are currently False (= must fix before tuning)."""
    out: dict[str, Any] = {}

    # 1. router_ticking
    try:
        from shared.auto_router import get_status  # noqa: WPS433
        s = get_status()
        out["router_ticking"] = bool(
            s.get("task_alive") and (int(s.get("tick_count") or 0) > 0)
        )
    except Exception:  # noqa: BLE001
        out["router_ticking"] = False

    # 2. lane_execution_enabled  (operator kill switch on /admin/lane-exec)
    try:
        from shared.lane_execution import is_lane_execution_enabled  # noqa: WPS433
        out["lane_execution_enabled"] = bool(await is_lane_execution_enabled(lane))
    except Exception:  # noqa: BLE001
        out["lane_execution_enabled"] = False

    # 3. broker_lane_enabled  (broker_lane_admin toggle, defaults True)
    try:
        from routes.broker_lane_admin import is_lane_enabled  # noqa: WPS433
        out["broker_lane_enabled"] = bool(await is_lane_enabled(lane))
    except Exception:  # noqa: BLE001
        out["broker_lane_enabled"] = False

    # 4. seat_assigned  (any brain holds an executor seat for this lane)
    try:
        from shared.executor_seat import get_seat_holder, seats_with_execute  # noqa: WPS433
        holders = []
        for seat_name in seats_with_execute(lane):
            h = await get_seat_holder(seat_name)
            if h:
                holders.append({"seat": seat_name, "brain": h})
        out["seat_assigned"] = bool(holders)
        out["seat_holders"] = holders
    except Exception:  # noqa: BLE001
        out["seat_assigned"] = False
        out["seat_holders"] = []

    # 5. broker_loaded  (adapter for the lane has credentials)
    try:
        if lane == "equity":
            from shared.broker.webull import get_webull_adapter  # noqa: WPS433
            adapter = await get_webull_adapter()
        elif lane == "crypto":
            from shared.crypto.broker_adapter import get_kraken_adapter  # noqa: WPS433
            adapter = await get_kraken_adapter()
        else:
            adapter = None
        out["broker_loaded"] = adapter is not None
    except Exception:  # noqa: BLE001
        out["broker_loaded"] = False

    # 6. direct_execute_mode
    try:
        from shared.direct_execute import is_direct_execute_enabled  # noqa: WPS433
        out["direct_execute_mode"] = bool(await is_direct_execute_enabled())
    except Exception:  # noqa: BLE001
        out["direct_execute_mode"] = False

    # 7. auto_submit_policy allows this lane + BUY/SELL
    try:
        from shared.auto_submit_policy import get_policy  # noqa: WPS433
        pol = get_policy()
        allowed_lanes = set(pol.get("allowed_lanes") or [])
        allowed_actions = set(pol.get("allowed_actions") or [])
        out["auto_submit_policy_ok"] = (
            lane in allowed_lanes
            and {"BUY", "SELL"}.issubset(allowed_actions)
        )
    except Exception:  # noqa: BLE001
        out["auto_submit_policy_ok"] = False

    # Roll-up: which structural checks are False.
    bool_check_keys = (
        "router_ticking", "lane_execution_enabled", "broker_lane_enabled",
        "seat_assigned", "broker_loaded", "direct_execute_mode",
        "auto_submit_policy_ok",
    )
    out["structural_blockers"] = [k for k in bool_check_keys if not out.get(k)]
    return out


# ─── Per-lane aggregator ───────────────────────────────────────────
async def _lane_report(lane: str, limit: int) -> dict[str, Any]:
    """Pull the last N intents in this lane and classify each.
    Returns the operator-facing report shape."""
    # Pull last N intents, newest first.
    intents = await db.shared_intents.find(
        {"lane": lane},
        {"_id": 0, "intent_id": 1, "action": 1, "symbol": 1,
         "gate_state": 1, "executed": 1, "ingest_ts": 1,
         "stack_canonical": 1, "confidence": 1},
    ).sort("ingest_ts", -1).max_time_ms(8000).to_list(limit)

    if not intents:
        return {
            "intents_seen": 0,
            "candidates": 0,
            "by_terminal_stage": {},
            "top_blocker": "NO_RECENT_INTENTS",
            "sample_blockers": [],
            "live_checks": await _live_checks_for_lane(lane),
        }

    stages: dict[str, int] = {}
    codes: dict[str, int] = {}
    samples: dict[str, list[dict]] = {}
    candidates = 0
    for it in intents:
        stage, code = await _classify_intent(it)
        stages[stage] = stages.get(stage, 0) + 1
        codes[code] = codes.get(code, 0) + 1
        if stage != "hold_bias":
            candidates += 1
        # Keep up to 3 sample intents per stage for the UI drilldown.
        if stage != "submitted":
            samples.setdefault(stage, [])
            if len(samples[stage]) < 3:
                samples[stage].append({
                    "intent_id": it.get("intent_id"),
                    "brain": it.get("stack_canonical"),
                    "symbol": it.get("symbol"),
                    "action": it.get("action"),
                    "confidence": it.get("confidence"),
                    "ingest_ts": it.get("ingest_ts"),
                    "code": code,
                })

    # Identify the dominant non-OK blocker as the headline.
    non_ok = {s: n for s, n in stages.items() if s not in ("submitted",)}
    if non_ok:
        top_stage = max(non_ok.items(), key=lambda kv: kv[1])[0]
    else:
        top_stage = "submitted"

    # The most common terminal CODE within the top stage — the actual
    # human-readable "why" the operator scans for first.
    top_code = None
    if top_stage != "submitted":
        # Look up which CODE is most common AMONG candidates with this stage.
        code_counts: dict[str, int] = {}
        for it in intents:
            stage, code = await _classify_intent(it)
            if stage == top_stage:
                code_counts[code] = code_counts.get(code, 0) + 1
        if code_counts:
            top_code = max(code_counts.items(), key=lambda kv: kv[1])[0]

    return {
        "intents_seen": len(intents),
        "candidates": candidates,
        "submitted": stages.get("submitted", 0),
        "by_terminal_stage": stages,
        "top_blocker_stage": top_stage,
        "top_blocker_code": top_code,
        "sample_blockers": samples,
        "live_checks": await _live_checks_for_lane(lane),
    }


@router.get("/unblock-report")
async def unblock_report(
    per_lane_limit: int = Query(default=100, ge=10, le=500),
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    """Why didn't the last N intents become orders, per lane?

    For each of {equity, crypto}:
      * intents_seen + candidates + submitted counts
      * by_terminal_stage histogram
      * top_blocker_stage + top_blocker_code (the headline)
      * sample_blockers per stage (drilldown)
      * live_checks (the 7 structural prerequisites)

    If `live_checks.structural_blockers` is non-empty, fix THOSE
    first. Tuning min_confidence / HOLD bias on a lane with a
    vacant executor seat or a disabled broker is a waste of time.
    """
    started = datetime.now(timezone.utc)
    equity = await _lane_report("equity", per_lane_limit)
    crypto = await _lane_report("crypto", per_lane_limit)
    elapsed_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
    return {
        "checked_at": started.isoformat(),
        "elapsed_ms": elapsed_ms,
        "per_lane_limit": per_lane_limit,
        "equity": equity,
        "crypto": crypto,
        "doctrine_note": (
            "live_checks.structural_blockers is the FIRST thing to fix "
            "per lane. Tuning strategy/confidence on a lane with a "
            "vacant seat or disabled broker is a waste of time."
        ),
    }
