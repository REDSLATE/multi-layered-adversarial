"""Per-intent inspection — answer "why is this intent in limbo?".

Doctrine pin (2026-02-18):
    The operator should never have to open Mongo to know why a
    specific intent_id is sitting at `gate_state=pending`. This
    endpoint runs the gate chain against a specified intent and
    returns the full pass/fail breakdown plus a hint about whether
    the failure is terminal (intent-frozen — will never pass) or
    transient (MC-state — might pass on a future tick).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from auth import get_current_user
from db import db
from namespaces import SHARED_INTENTS


router = APIRouter(prefix="/admin/intent", tags=["intent-inspect"])


# Gates whose failure is FROZEN on the intent doc itself. Their inputs
# don't change between ticks, so a fail here is terminal-for-this-
# intent. The operator can safely conclude "this row will never fire,
# tell the brain to re-emit if it still has the opinion".
INTENT_FROZEN_GATES = {
    "schema_invariants",
    "action_routable",
    "executor_seat_check",
    "roadguard_spread_floor",
}

# Gates whose failure depends on MC's LIVE state. The intent might pass
# on a future tick if MC's state changes (broker reconnects, operator
# flips toggle, governor seat refills, etc.).
MC_STATE_GATES = {
    "live_trading_disabled",
    "broker_connected",
    "lane_execution_enabled",
    "governor_authority",
    "opponent_objection",
    "cap_per_order",
    "cap_per_order_crypto",
    "cap_per_day",
    "cap_open_notional",
}


@router.get("/{intent_id}/inspect")
async def inspect_intent(
    intent_id: str,
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    """Run the gate chain against a specific intent and return the
    full breakdown with a terminal-vs-transient hint per failure."""
    intent = await db[SHARED_INTENTS].find_one(
        {"intent_id": intent_id}, {"_id": 0},
    )
    if intent is None:
        raise HTTPException(status_code=404, detail=f"intent {intent_id!r} not found")

    from shared.execution import _evaluate_gates  # noqa: WPS433
    # Use the auto-router's default notional so the inspection mirrors
    # what the worker would actually run.
    import os
    notional = float(os.environ.get("AUTO_ROUTER_NOTIONAL_USD", "100"))
    result = await _evaluate_gates(intent, notional)

    gates_out = []
    for g in result.get("gates", []):
        gate_name = g.get("name") or ""
        is_terminal = (
            not g.get("passed")
            and gate_name in INTENT_FROZEN_GATES
        )
        is_transient = (
            not g.get("passed")
            and gate_name in MC_STATE_GATES
        )
        gates_out.append({
            **g,
            "failure_kind": (
                "terminal" if is_terminal
                else "transient" if is_transient
                else None
            ),
        })

    # Compose a one-line operator summary.
    first_block = next((g for g in gates_out if not g.get("passed")), None)
    if first_block is None:
        summary = "all gates pass — intent would route on next tick"
    elif first_block.get("failure_kind") == "terminal":
        summary = (
            f"terminal block at `{first_block['name']}` — intent will "
            f"NEVER pass; brain must re-emit with corrected inputs"
        )
    elif first_block.get("failure_kind") == "transient":
        summary = (
            f"transient block at `{first_block['name']}` — depends on "
            f"MC state; might pass on a future tick"
        )
    else:
        summary = f"blocked at `{first_block['name']}`"

    return {
        "intent_id": intent_id,
        "stack": intent.get("stack"),
        "symbol": intent.get("symbol"),
        "action": intent.get("action"),
        "lane": intent.get("lane"),
        "current_gate_state": intent.get("gate_state"),
        "executed": intent.get("executed"),
        "ingest_ts": intent.get("ingest_ts"),
        "holds_executor_seat": intent.get("holds_executor_seat"),
        "executor_holder_at_post": intent.get("executor_holder_at_post"),
        "snapshot": intent.get("snapshot"),
        "live_gate_chain": gates_out,
        "first_blocker": first_block,
        "summary": summary,
        "doctrine_note": (
            "Read-only inspection. Does NOT mutate gate_state. Use the "
            "auto_router or POST /admin/intent/{id}/dispose to actually "
            "flip terminal limbo intents to blocked."
        ),
    }


@router.post("/{intent_id}/dispose")
async def dispose_intent(
    intent_id: str,
    user: dict = Depends(get_current_user),  # noqa: B008
):
    """Operator-forced terminal disposition. Flips a pending intent to
    `gate_state=blocked` with an operator-attributed reason. Useful for
    manually draining limbo without waiting for the auto_router's
    sweep (e.g., very old intents that pre-date the sweep)."""
    intent = await db[SHARED_INTENTS].find_one(
        {"intent_id": intent_id}, {"_id": 0, "gate_state": 1, "stack": 1},
    )
    if intent is None:
        raise HTTPException(status_code=404, detail=f"intent {intent_id!r} not found")
    if intent.get("gate_state") not in ("pending", None):
        raise HTTPException(
            status_code=400,
            detail=f"intent gate_state={intent.get('gate_state')!r} — only `pending` may be operator-disposed",
        )
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    actor = user.get("email") or "operator"
    await db[SHARED_INTENTS].update_one(
        {"intent_id": intent_id},
        {"$set": {
            "gate_state": "blocked",
            "last_submit_ts": now,
            "last_submit_by": actor,
            "operator_disposed": True,
            "operator_dispose_reason": "manual_terminal_dispose",
        }},
    )
    return {
        "ok": True,
        "intent_id": intent_id,
        "previous": intent.get("gate_state"),
        "current": "blocked",
        "actor": actor,
    }
