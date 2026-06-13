"""Paradox v2 `/evaluate` pipeline — five-layer execution decision.

Stages:
    1. SEAT POLICY     → trust check, capital gates, autonomy mode
    2. GOVERNOR        → structured size modifiers (never blocks)
    3. ROADGUARD       → binary STOP check
    4. EXEC DECISION   → assemble final notional + decision
    5. VERIFIER (out)  → write receipt; verifier ingests later

Decision codes:
    EXECUTED            — passed all gates; ready for broker submit
    REJECTED_SEAT       — seat refused (trust, confidence, capital, etc.)
    REJECTED_ROADGUARD  — RoadGuard binary STOP active
    BLOCKED             — seat in observe/shadow mode (paper only)
    PENDING_VOTE        — a governor rule flagged vote_required=true

Stand-alone deployment: this pipeline does NOT submit orders. It returns
a decision receipt; the caller (admin UI or future intent-pipeline wire)
is responsible for routing to /api/execution/submit if `EXECUTED`.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from db import db
from namespaces import (
    PARADOX_V2_EVALUATIONS,
    PARADOX_V2_GOVERNOR_RULES,
    PARADOX_V2_ROADGUARD_STOPS,
    PARADOX_V2_SEAT_POLICY,
    PARADOX_V2_SEAT_TRUSTED,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ─── Stage 1: SEAT POLICY ─────────────────────────────────────────────


async def _stage_seat_policy(
    opinion: dict[str, Any], seat_id: str,
) -> dict[str, Any]:
    """Returns {pass, reason, policy, trust, autonomy_mode}.

    The seat checks (in order):
      a. seat_policy_config row exists and enabled
      b. brain is in seat_trusted_brains
      c. opinion.confidence >= seat.confidence_min
      d. opinion.suggested_notional <= seat.max_notional (pre-multiplier)
    """
    policy = await db[PARADOX_V2_SEAT_POLICY].find_one(
        {"seat_id": seat_id}, {"_id": 0},
    )
    if not policy:
        return {"pass": False, "reason": f"unknown_seat: {seat_id}",
                "policy": None, "trust": None}
    if not policy.get("enabled", False):
        return {"pass": False, "reason": "seat_disabled",
                "policy": policy, "trust": None}

    trust = await db[PARADOX_V2_SEAT_TRUSTED].find_one(
        {"seat_id": seat_id, "brain_id": opinion["brain_id"]}, {"_id": 0},
    )
    if not trust:
        return {"pass": False,
                "reason": f"brain_not_trusted: {opinion['brain_id']} not in {seat_id} trust list",
                "policy": policy, "trust": None}

    conf = float(opinion.get("confidence") or 0.0)
    if conf < policy["confidence_min"]:
        return {"pass": False,
                "reason": f"confidence_below_floor: {conf:.3f} < {policy['confidence_min']:.3f}",
                "policy": policy, "trust": trust}

    suggested = float(opinion.get("suggested_notional_usd") or 0.0)
    if suggested > policy["max_notional_usd"]:
        return {"pass": False,
                "reason": f"notional_exceeds_seat_cap: ${suggested:.2f} > ${policy['max_notional_usd']:.2f}",
                "policy": policy, "trust": trust}

    return {
        "pass": True, "reason": "seat_policy_pass",
        "policy": policy, "trust": trust,
        "autonomy_mode": policy.get("autonomy_mode", "observe"),
    }


# ─── Stage 2: GOVERNOR ────────────────────────────────────────────────


async def _stage_governor(
    opinion: dict[str, Any],
) -> dict[str, Any]:
    """Returns {size_multiplier, vote_required, applied_rules, reasons}.

    Pulls every active rule, checks `trigger_type` against the opinion's
    `evidence` dict. The brain is responsible for embedding raw signals
    (spread_bps, rvol, earnings_within_days, …) in `evidence`. The
    governor turns those signals into a structured size adjustment.

    Multiplicative composition — multiple firing rules COMPOUND. e.g.
    wide_spread (0.5) + earnings_window (0.25) → final 0.125.
    """
    rules = await db[PARADOX_V2_GOVERNOR_RULES].find(
        {"is_active": True}, {"_id": 0},
    ).to_list(100)

    evidence = opinion.get("evidence") or {}
    applied: list[dict[str, Any]] = []
    final_mult = 1.0
    vote_required = False

    for r in rules:
        trigger = r["trigger_type"]
        threshold = float(r.get("trigger_threshold", 0.0))
        fired = False
        formatted_reason = r.get("reason_template", "")

        if trigger == "wide_spread":
            spread = _to_float(evidence.get("spread_bps"))
            if spread is not None and spread >= threshold:
                fired = True
                formatted_reason = formatted_reason.format(spread_bps=spread)
        elif trigger == "low_rvol":
            rvol = _to_float(evidence.get("rvol"))
            if rvol is not None and rvol <= threshold:
                fired = True
                formatted_reason = formatted_reason.format(rvol=rvol)
        elif trigger == "earnings_window":
            in_window = bool(evidence.get("earnings_within_days", 0))
            if in_window:
                fired = True
                formatted_reason = r.get("reason_template", "Earnings window flagged.")
        elif trigger == "halt_risk":
            if bool(evidence.get("halt_risk", False)):
                fired = True
                formatted_reason = r.get("reason_template", "Halt risk flagged.")
        # Unknown trigger types are ignored — the governor never
        # implicitly fires on signals it doesn't understand.

        if fired:
            final_mult *= float(r.get("size_multiplier", 1.0))
            if r.get("vote_required"):
                vote_required = True
            applied.append({
                "rule_id": r["rule_id"],
                "trigger_type": trigger,
                "size_multiplier": float(r.get("size_multiplier", 1.0)),
                "vote_required": bool(r.get("vote_required", False)),
                "reason": formatted_reason,
            })

    return {
        "size_multiplier": round(final_mult, 6),
        "vote_required": vote_required,
        "applied_rules": applied,
        "reasons": [a["reason"] for a in applied],
    }


def _to_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ─── Stage 3: ROADGUARD ───────────────────────────────────────────────


async def _stage_roadguard(seat_id: str) -> dict[str, Any]:
    """Returns {status: OPEN|BLOCKED, reason, stop_id?}.

    Pure binary read of the active stops for this seat.
    """
    stop = await db[PARADOX_V2_ROADGUARD_STOPS].find_one(
        {"seat_id": seat_id, "is_active": True, "cleared_at": None},
        {"_id": 0},
        sort=[("created_at", -1)],
    )
    if stop:
        return {
            "status": "BLOCKED",
            "reason": stop.get("reason", "roadguard_stop_active"),
            "stop_at": stop.get("created_at"),
            "triggered_by": stop.get("triggered_by"),
        }
    return {"status": "OPEN", "reason": "no_active_stop"}


# ─── Pipeline orchestrator ────────────────────────────────────────────


async def evaluate(opinion: dict[str, Any], seat_id: str) -> dict[str, Any]:
    """Run the full five-stage pipeline. Always persists a receipt.

    Returns the receipt dict directly (also persisted to
    PARADOX_V2_EVALUATIONS).
    """
    evaluation_id = str(uuid.uuid4())
    trace: dict[str, Any] = {}

    # Stage 1: seat
    seat_res = await _stage_seat_policy(opinion, seat_id)
    trace["seat_policy"] = {
        "pass": seat_res["pass"],
        "reason": seat_res["reason"],
        "autonomy_mode": seat_res.get("autonomy_mode"),
    }
    if not seat_res["pass"]:
        return await _persist(evaluation_id, seat_id, opinion,
                              "REJECTED_SEAT", seat_res["reason"],
                              None, trace)

    # Stage 2: governor
    gov_res = await _stage_governor(opinion)
    trace["governor"] = gov_res

    # Stage 3: roadguard
    rg_res = await _stage_roadguard(seat_id)
    trace["roadguard"] = rg_res
    if rg_res["status"] == "BLOCKED":
        return await _persist(evaluation_id, seat_id, opinion,
                              "REJECTED_ROADGUARD", rg_res["reason"],
                              None, trace)

    # Stage 4: assemble exec decision
    policy = seat_res["policy"]
    suggested = float(opinion.get("suggested_notional_usd") or 0.0)
    final_notional = round(
        suggested * float(policy["size_multiplier"]) * float(gov_res["size_multiplier"]),
        2,
    )
    # Hard cap at seat's max_notional_usd post-multipliers.
    if final_notional > policy["max_notional_usd"]:
        final_notional = float(policy["max_notional_usd"])
    trace["exec_assembly"] = {
        "suggested_notional": suggested,
        "seat_size_multiplier": float(policy["size_multiplier"]),
        "governor_size_multiplier": float(gov_res["size_multiplier"]),
        "final_notional_usd": final_notional,
        "seat_cap_applied": final_notional == float(policy["max_notional_usd"]),
    }

    # Decision branching
    if gov_res["vote_required"]:
        decision = "PENDING_VOTE"
        reason = "governor_flagged_vote_required: " + "; ".join(gov_res["reasons"])
    elif seat_res["autonomy_mode"] in ("observe", "shadow"):
        decision = "BLOCKED"
        reason = f"seat_in_{seat_res['autonomy_mode']}_mode: paper-only until promoted"
    else:
        decision = "EXECUTED"
        reason = "all_gates_pass"

    return await _persist(evaluation_id, seat_id, opinion,
                          decision, reason, final_notional, trace)


async def _persist(
    evaluation_id: str,
    seat_id: str,
    opinion: dict[str, Any],
    decision: str,
    reason: str,
    final_notional: float | None,
    trace: dict[str, Any],
) -> dict[str, Any]:
    receipt = {
        "evaluation_id": evaluation_id,
        "seat_id": seat_id,
        "opinion": opinion,
        "decision": decision,
        "reason": reason,
        "final_notional_usd": final_notional,
        "pipeline_trace": trace,
        "ts": _now(),
    }
    await db[PARADOX_V2_EVALUATIONS].insert_one(dict(receipt))
    receipt.pop("_id", None)
    return receipt
