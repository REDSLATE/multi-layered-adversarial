"""GET /api/admin/equity-trade-readiness — single diagnostic endpoint.

Operator brief (2026-02-25):
    Show me, for each recent equity intent, the full authority chain
    (raw_action → normalized_action → broker_action → submit_allowed)
    AND the ordered list of which gate stopped it AND the
    `first_failing_gate` so I can see at a glance whether equity is
    blocked by brain_hold, seat_holder, market_hours, dry_run,
    consensus, action_allowed, or roadguard.

Constraint pinned by operator:
    "Don't let this endpoint recompute doctrine. It should report
     what happened from persisted intent/audit fields as much as
     possible. Recomputing can create a second truth."

So this module is a JOIN + reshape over already-persisted data:
    - `shared_intents`    : raw_action, display_action, market_decision,
                            confidence, gate_state, dry_run_state,
                            dry_run_reason, hold_reason, target_price,
                            stop_price, would_have_traded_without_gates
    - `pipeline_receipts` : final_status, final_reason, restriction_source,
                            broker_called, consensus snapshot
    - `shared_gate_results`: per-gate verdict rows (dry_run, submit_blocked,
                            submit_requires_override, auto_submit_skipped)
    - Current global state (read-only, used to compute "would this fire NOW"):
        * `get_seat_holder("executor")`
        * `is_equity_rth() / is_equity_extended_hours()`
        * `get_policy()` — current allowed_actions / dry_run requirement

Nothing in this file re-evaluates a doctrine. The `broker_action`
mapping is a pure projection of what the broker layer would
accept on a Webull cash account (BUY/SELL pass; SHORT/COVER are
margin-only and not in `allowed_actions` today). The mapping is
explicitly labeled `source: "diagnostic_projection_cash_account"`
so the operator knows it's not pulled from a persisted broker
submission log.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query

from auth import get_current_user
from db import db
from shared.auto_submit_policy import get_policy
from shared.executor_seat import get_seat_holder
from shared.lane_execution import is_lane_execution_enabled
from shared.market_hours import (
    is_equity_extended_hours,
    is_equity_rth,
    next_rth_open_iso,
)


router = APIRouter(prefix="/admin", tags=["equity-trade-readiness"])


# ─────────────────────────── constants ─────────────────────────────

PIPELINE_RECEIPTS = "pipeline_receipts"
SHARED_INTENTS = "shared_intents"
SHARED_GATE_RESULTS = "shared_gate_results"

# Gate ordering — the authority chain. Operator-pinned 2026-02-25:
#   brain_hold → seat_holder → market_hours → dry_run → consensus
#   → action_allowed → rr_validity → roadguard
# The order matters: this is what `first_failing_gate` walks. A gate
# only fires (FAIL) if every earlier gate PASSed; otherwise it's SKIP.
_GATE_ORDER = (
    "brain_hold",
    "seat_holder",
    "market_hours",
    "dry_run",
    "consensus",
    "action_allowed",
    "rr_validity",
    "roadguard",
)


# Broker-action projection for a Webull cash account. NOT persisted
# anywhere — this is a pure reshape so the operator can see the
# SHORT/COVER mismatch the auditor flagged in a single field.
def _project_broker_action(normalized_action: Optional[str]) -> Optional[str]:
    """Project the normalized intent action to what the broker layer
    would actually fire on a Webull cash account.

    Cash account doctrine:
        BUY   → BUY    (long open)
        SELL  → SELL   (long close)
        SHORT → None   (requires margin; auto_submit blocks)
        COVER → None   (no short to cover on cash account)
        HOLD  → None   (no order)
    """
    if normalized_action == "BUY":
        return "BUY"
    if normalized_action == "SELL":
        return "SELL"
    return None


# ─────────────────────── per-intent enrichment ──────────────────────


def _market_hours_verdict(
    ingest_ts: Optional[str], extended_enabled_now: bool,
) -> dict[str, Any]:
    """Was the intent emitted during a window where it could trade?

    Uses the intent's `ingest_ts` to project past-state. Does NOT
    rebuild the RTH calendar — just delegates to `is_equity_rth /
    is_equity_extended_hours` which are pure functions on a datetime.
    """
    if not ingest_ts:
        return {"state": "unknown", "verdict": "SKIP", "reason": "missing_ingest_ts"}
    try:
        t = datetime.fromisoformat(ingest_ts.replace("Z", "+00:00"))
    except ValueError:
        return {"state": "unparseable", "verdict": "SKIP", "reason": f"ingest_ts={ingest_ts!r}"}
    rth = is_equity_rth(t)
    ext = is_equity_extended_hours(t)
    if rth:
        state = "RTH"
        verdict = "PASS"
    elif ext and extended_enabled_now:
        state = "EXT_HOURS_ENABLED"
        verdict = "PASS"
    elif ext and not extended_enabled_now:
        state = "EXT_HOURS_DISABLED"
        verdict = "FAIL"
    else:
        state = "CLOSED"
        verdict = "FAIL"
    return {"state": state, "verdict": verdict, "ingest_ts": ingest_ts}


def _brain_hold_verdict(intent: dict[str, Any]) -> dict[str, Any]:
    """Did the brain itself emit HOLD?

    Reads from `display_action` + `hold_reason` directly. Pure
    persisted-field read — no doctrine recompute."""
    display = intent.get("display_action") or intent.get("action")
    if display == "HOLD":
        return {
            "verdict": "FAIL",
            "reason": intent.get("hold_reason") or "unspecified",
            "would_have_traded_without_gates": bool(
                intent.get("would_have_traded_without_gates"),
            ),
        }
    return {"verdict": "PASS", "display_action": display}


def _seat_verdict(intent: dict[str, Any], current_seat: Optional[str]) -> dict[str, Any]:
    """Is the emitting brain the current equity executor seat holder?

    NOTE: this compares the intent's `stack` (emitter) to the CURRENT
    seat holder — not the historical one at emission time, because
    the seat assignment is not stamped on the intent doc. If the
    operator rotated the seat between emission and now, the verdict
    will reflect the current truth. This is the same trade-off
    `intent_why` makes.
    """
    emitter = intent.get("stack")
    if not current_seat:
        return {"verdict": "FAIL", "reason": "no_seat_holder", "emitter": emitter, "current_seat": None}
    if emitter == current_seat:
        return {"verdict": "PASS", "emitter": emitter, "current_seat": current_seat}
    return {
        "verdict": "FAIL",
        "reason": "emitter_is_not_seat_holder",
        "emitter": emitter,
        "current_seat": current_seat,
    }


def _dry_run_verdict(intent: dict[str, Any]) -> dict[str, Any]:
    """Read `dry_run_state` + `dry_run_reason` from the intent. The
    earlier 2026-02-23 instrumentation fix ensures `dry_run_reason`
    is populated when state=blocked."""
    state = intent.get("dry_run_state")
    if state == "passed":
        return {"verdict": "PASS", "state": "passed"}
    if state == "blocked":
        return {
            "verdict": "FAIL",
            "state": "blocked",
            "reason": intent.get("dry_run_reason") or "not_populated",
        }
    return {
        "verdict": "SKIP",
        "state": state or "missing",
        "reason": "dry_run_did_not_run",
    }


def _rr_verdict(intent: dict[str, Any]) -> dict[str, Any]:
    """Does the intent have coherent target/stop prices relative to
    the action direction?

    Pure structural check on the persisted prices — does NOT recompute
    the R:R doctrine math. If the operator's R:R policy changes, this
    check still reflects the actual numbers the brain wrote."""
    target = intent.get("target_price")
    stop = intent.get("stop_price")
    action = intent.get("raw_action") or intent.get("action")
    if target is None or stop is None:
        return {"verdict": "SKIP", "reason": "rr_prices_not_set", "target": target, "stop": stop}
    if action in ("BUY",) and not (stop < target):
        return {
            "verdict": "FAIL",
            "reason": f"BUY requires stop<target, got stop={stop} target={target}",
            "target": target, "stop": stop, "action": action,
        }
    if action in ("SHORT", "SELL") and not (stop > target):
        return {
            "verdict": "FAIL",
            "reason": f"{action} requires stop>target, got stop={stop} target={target}",
            "target": target, "stop": stop, "action": action,
        }
    return {"verdict": "PASS", "target": target, "stop": stop, "action": action}


def _consensus_verdict(receipt: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Read the consensus snapshot from the pipeline receipt. If the
    receipt didn't store one (pre-consensus pipeline or kill-switch
    on), report SKIP."""
    if not receipt:
        return {"verdict": "SKIP", "reason": "no_pipeline_receipt"}
    c = receipt.get("consensus")
    if not c or not isinstance(c, dict):
        return {"verdict": "SKIP", "reason": "no_consensus_snapshot"}
    base = c.get("base_confidence")
    eff = c.get("effective_confidence")
    boost = c.get("advisor_boost")
    # Read final_status to see if consensus was the blocking source.
    # restriction_source values from PipelineReceipt model: brain | seat | roadguard | broker
    # Consensus blocks usually surface as restriction_source="brain" with reason like "consensus_pushed_below_floor".
    src = receipt.get("restriction_source")
    reason = receipt.get("final_reason") or ""
    blocked_by_consensus = (
        "consensus" in reason.lower() or "below_floor" in reason.lower()
    ) and src == "brain"
    return {
        "verdict": "FAIL" if blocked_by_consensus else "PASS",
        "base_confidence": base,
        "effective_confidence": eff,
        "advisor_boost": boost,
        "reason": reason if blocked_by_consensus else None,
    }


def _action_allowed_verdict(
    broker_action: Optional[str], allowed_actions: list[str],
) -> dict[str, Any]:
    """Pure projection. The auto_submit policy's `allowed_actions`
    is the canonical filter. If broker_action is None (SHORT/COVER
    on cash, or HOLD), the intent cannot fire."""
    if broker_action is None:
        return {
            "verdict": "FAIL",
            "reason": "broker_action_null",
            "broker_action": None,
            "allowed_actions": allowed_actions,
        }
    if broker_action in allowed_actions:
        return {
            "verdict": "PASS",
            "broker_action": broker_action,
            "allowed_actions": allowed_actions,
        }
    return {
        "verdict": "FAIL",
        "reason": f"{broker_action!r} not in allowed_actions",
        "broker_action": broker_action,
        "allowed_actions": allowed_actions,
    }


def _roadguard_verdict(receipt: Optional[dict[str, Any]]) -> dict[str, Any]:
    """RoadGuard surfaces as `restriction_source="roadguard"` on the
    pipeline receipt. No re-evaluation here."""
    if not receipt:
        return {"verdict": "SKIP", "reason": "no_pipeline_receipt"}
    src = receipt.get("restriction_source")
    if src == "roadguard":
        return {"verdict": "FAIL", "reason": receipt.get("final_reason") or "roadguard_block"}
    return {"verdict": "PASS"}


# ───────────────────────── ordering helper ──────────────────────────


def _first_failing_gate(gate_results: dict[str, dict[str, Any]]) -> Optional[str]:
    """Walk `_GATE_ORDER` and return the name of the first gate
    whose verdict is FAIL. SKIP and PASS continue. None means
    everything PASSed (or only SKIPped)."""
    for name in _GATE_ORDER:
        v = gate_results.get(name) or {}
        if v.get("verdict") == "FAIL":
            return name
    return None


# ───────────────────────── route handler ────────────────────────────


@router.get("/equity-trade-readiness")
async def equity_trade_readiness(
    symbol: Optional[str] = Query(default=None, description="filter to one symbol"),
    limit: int = Query(default=20, ge=1, le=200),
    hours: int = Query(default=24, ge=1, le=168),
    _user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    """Single-shot diagnostic for "why isn't equity trading?"

    Read-only. Does NOT recompute doctrine — every verdict is derived
    from persisted fields on `shared_intents` / `pipeline_receipts` /
    `shared_gate_results` plus current global state for the
    seat / market-hours / policy projection.
    """
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(hours=hours)).isoformat()

    # ── current global state (used for "would this fire NOW" projection) ──
    current_seat = await get_seat_holder("executor")
    policy = get_policy()
    allowed_actions = list(policy.get("allowed_actions") or [])
    rth_now = is_equity_rth(now)
    ext_now = is_equity_extended_hours(now)
    # 2026-02-25 (op-correction): the previous read pulled this off
    # `policy.equity_extended_hours_enabled` which is NOT the source
    # of truth — the operator toggles it via the admin route, which
    # writes to `runtime_flags._id="equity_extended_hours"`. Pull it
    # directly so prod's actual flag state is reflected, not a stale
    # policy field that was never wired.
    from routes.equity_extended_hours_admin import (
        get_equity_extended_hours_enabled,
    )  # noqa: WPS433 — late import avoids router_registry cycle
    extended_enabled_now = await get_equity_extended_hours_enabled()
    # Lane-execution toggle (the operator's master switch per lane).
    # When OFF, even an in-RTH equity intent will be held by the
    # `lane_execution_enabled` gate downstream.
    lane_enabled = await is_lane_execution_enabled("equity")

    # Compose a single operator-facing `lane_status`:
    #   OPEN     — lane toggle ON AND (in RTH, OR (in ext-hrs window
    #              AND ext-hrs toggle ON))
    #   GATED    — lane toggle ON but market currently closed and no
    #              ext-hrs coverage. Intents are emitted but held by
    #              the market-hours gate until next RTH open.
    #   DISABLED — lane toggle is OFF. The lane is the closed thing,
    #              not the market.
    if not lane_enabled:
        lane_status = "DISABLED"
    elif rth_now or (ext_now and extended_enabled_now):
        lane_status = "OPEN"
    else:
        lane_status = "GATED"

    session = {
        "now_utc": now.isoformat(),
        "rth": rth_now,
        "extended_hours_window": ext_now,
        "extended_hours_enabled": extended_enabled_now,
        "lane_enabled": lane_enabled,
        "lane_status": lane_status,
        "next_rth_open_iso": next_rth_open_iso(now),
    }

    # ── latest equity intents ──
    q: dict[str, Any] = {"lane": "equity", "ingest_ts": {"$gte": cutoff}}
    if symbol:
        q["symbol"] = symbol.strip().upper()

    intents = await db[SHARED_INTENTS].find(
        q, {"_id": 0},
    ).sort("ingest_ts", -1).max_time_ms(15000).to_list(limit)

    # Batch-fetch matching pipeline receipts (single query, not N+1).
    intent_ids = [i.get("intent_id") for i in intents if i.get("intent_id")]
    receipts_by_id: dict[str, dict[str, Any]] = {}
    if intent_ids:
        cursor = db[PIPELINE_RECEIPTS].find(
            {"intent_id": {"$in": intent_ids}}, {"_id": 0},
        ).max_time_ms(15000)
        async for r in cursor:
            receipts_by_id[r["intent_id"]] = r

    # ── reshape each intent into the readiness row ──
    items: list[dict[str, Any]] = []
    for intent in intents:
        raw_action = intent.get("raw_action") or intent.get("action")
        normalized_action = intent.get("action")  # post-_normalize_action
        broker_action = _project_broker_action(normalized_action)
        display_action = intent.get("display_action")
        receipt = receipts_by_id.get(intent.get("intent_id") or "")

        gates: dict[str, dict[str, Any]] = {
            "brain_hold": _brain_hold_verdict(intent),
            "seat_holder": _seat_verdict(intent, current_seat),
            "market_hours": _market_hours_verdict(
                intent.get("ingest_ts"), extended_enabled_now,
            ),
            "dry_run": _dry_run_verdict(intent),
            "consensus": _consensus_verdict(receipt),
            "action_allowed": _action_allowed_verdict(broker_action, allowed_actions),
            "rr_validity": _rr_verdict(intent),
            "roadguard": _roadguard_verdict(receipt),
        }
        # Convert dict→ordered list for stable client iteration.
        ordered_blockers = [
            {"gate": name, **(gates.get(name) or {})}
            for name in _GATE_ORDER
        ]

        items.append({
            "intent_id": intent.get("intent_id"),
            "ingest_ts": intent.get("ingest_ts"),
            "stack": intent.get("stack"),
            "symbol": intent.get("symbol"),
            "lane": intent.get("lane"),
            "confidence": intent.get("confidence"),
            "raw_confidence": intent.get("raw_confidence"),
            # ── Authority chain (operator-pinned vocabulary) ──
            "raw_action": raw_action,
            "normalized_action": normalized_action,
            "broker_action": broker_action,
            "display_action": display_action,
            "translation": {
                "raw_action": raw_action,
                "normalized_action": normalized_action,
                "broker_action": broker_action,
                "source": "diagnostic_projection_cash_account",
            },
            # ── Persisted truth (no recompute) ──
            "gate_state": intent.get("gate_state"),
            "pipeline_final_status": (receipt or {}).get("final_status"),
            "pipeline_final_reason": (receipt or {}).get("final_reason"),
            "pipeline_restriction_source": (receipt or {}).get("restriction_source"),
            "broker_called": bool((receipt or {}).get("broker_called", False)),
            # ── Ordered blocker chain + single-answer field ──
            "blockers": ordered_blockers,
            "first_failing_gate": _first_failing_gate(gates),
        })

    # ── fleet 24h histogram (separate query — not limited by `limit`) ──
    histogram_intents_cursor = db[SHARED_INTENTS].find(
        {"lane": "equity", "ingest_ts": {"$gte": cutoff}},
        {
            "intent_id": 1, "stack": 1, "action": 1, "raw_action": 1,
            "display_action": 1, "dry_run_state": 1, "dry_run_reason": 1,
            "hold_reason": 1, "ingest_ts": 1, "target_price": 1,
            "stop_price": 1, "_id": 0,
        },
    ).max_time_ms(15000)
    hist_intents = await histogram_intents_cursor.to_list(length=10000)
    hist_ids = [i.get("intent_id") for i in hist_intents if i.get("intent_id")]
    hist_receipts: dict[str, dict[str, Any]] = {}
    if hist_ids:
        async for r in db[PIPELINE_RECEIPTS].find(
            {"intent_id": {"$in": hist_ids}}, {"_id": 0},
        ).max_time_ms(15000):
            hist_receipts[r["intent_id"]] = r

    counter: Counter[str] = Counter()
    total = 0
    for it in hist_intents:
        total += 1
        bra = _project_broker_action(it.get("action"))
        gs = {
            "brain_hold": _brain_hold_verdict(it),
            "seat_holder": _seat_verdict(it, current_seat),
            "market_hours": _market_hours_verdict(
                it.get("ingest_ts"), extended_enabled_now,
            ),
            "dry_run": _dry_run_verdict(it),
            "consensus": _consensus_verdict(hist_receipts.get(it.get("intent_id") or "")),
            "action_allowed": _action_allowed_verdict(bra, allowed_actions),
            "rr_validity": _rr_verdict(it),
            "roadguard": _roadguard_verdict(hist_receipts.get(it.get("intent_id") or "")),
        }
        first = _first_failing_gate(gs)
        counter[first or "all_pass"] += 1

    return {
        "now": now.isoformat(),
        "window_hours": hours,
        "filter": {"symbol": symbol},
        "session": session,
        "seat": {
            "equity_executor": current_seat,
            "source": "shared_brain_roster.assignments[executor]",
        },
        "policy": {
            "auto_submit_enabled": bool(policy.get("enabled", False)),
            "tier_source": policy.get("source"),
            "allowed_actions": allowed_actions,
            "allowed_brains": policy.get("allowed_brains") or [],
            "required_dry_run_state": policy.get("required_dry_run_state"),
            "notional_max_usd": policy.get("notional_max_usd"),
        },
        "gate_order": list(_GATE_ORDER),
        "items": items,
        "count": len(items),
        "fleet_summary": {
            "total_intents_window": total,
            "by_first_failing_gate": dict(counter.most_common()),
        },
    }


# 2026-02-25 (later) — equity dry-run autopsy endpoint.
# The fleet histogram in `/equity-trade-readiness` tells you the
# top failing GATE family. This endpoint drills inside the
# `dry_run` family and names which of the ~12 dry-run sub-gates
# is actually firing. `shared_intents.dry_run_reason` has the
# answer per-intent in the format `"<gate_name>:<reason_text>"`.
# Aggregating that field gives the operator a one-shot answer
# to "what's blocking trades inside the dry-run."

@router.get("/equity-dry-run-autopsy")
async def equity_dry_run_autopsy(
    hours: int = Query(default=24, ge=1, le=168),
    _user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    """Read-only autopsy of `dry_run_reason` for blocked equity
    intents in the window. Returns:
      - total_dry_run_blocked
      - by_gate_name (the gate that fired, e.g. "broker_connected")
      - by_full_reason (top 20 full reason strings, includes the
        specific "...not connected for lane='equity'" detail)
      - sample_intents (3 newest per top gate so the operator can
        verify the reason matches the situation)
    """
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(hours=hours)).isoformat()

    q = {
        "lane": "equity",
        "ingest_ts": {"$gte": cutoff},
        "dry_run_state": "dry_run_blocked",
    }

    total = await db[SHARED_INTENTS].count_documents(q)

    by_gate: dict[str, int] = {}
    by_reason: dict[str, int] = {}
    sample_by_gate: dict[str, list[dict[str, Any]]] = {}

    cursor = db[SHARED_INTENTS].find(
        q,
        {
            "_id": 0, "intent_id": 1, "stack": 1, "symbol": 1,
            "action": 1, "ingest_ts": 1,
            "dry_run_reason": 1, "target_price": 1, "stop_price": 1,
        },
    ).sort("ingest_ts", -1).max_time_ms(15000)

    async for row in cursor:
        reason = row.get("dry_run_reason") or "<no reason persisted>"
        # Split "gate_name:specific_text" → ("gate_name", "specific_text")
        if ":" in reason:
            gate_name = reason.split(":", 1)[0].strip()
        else:
            gate_name = reason.strip()
        by_gate[gate_name] = by_gate.get(gate_name, 0) + 1
        by_reason[reason] = by_reason.get(reason, 0) + 1
        bucket = sample_by_gate.setdefault(gate_name, [])
        if len(bucket) < 3:
            bucket.append({
                "intent_id": row.get("intent_id"),
                "brain": row.get("stack"),
                "symbol": row.get("symbol"),
                "action": row.get("action"),
                "ingest_ts": row.get("ingest_ts"),
                "target_price": row.get("target_price"),
                "stop_price": row.get("stop_price"),
                "dry_run_reason": reason,
            })

    by_gate_sorted = sorted(by_gate.items(), key=lambda x: -x[1])
    by_reason_sorted = sorted(by_reason.items(), key=lambda x: -x[1])[:20]

    return {
        "now": now.isoformat(),
        "window_hours": hours,
        "total_dry_run_blocked": total,
        "by_gate_name": [{"gate": g, "n": n} for g, n in by_gate_sorted],
        "by_full_reason_top20": [{"reason": r, "n": n} for r, n in by_reason_sorted],
        "sample_intents_by_gate": sample_by_gate,
    }
