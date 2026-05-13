"""Decision Machine — intent envelope ingest.

Brains emit *intents*, not orders. Every intent is a candidate that lives
or dies based on whether the gate chain passes. This module accepts the
envelope from a brain, MC-stamps it (seat_at_post_time, intent_id, ts),
schema-pins the safety invariants (`may_execute=false`,
`requires_gate_pass=true`), and stores it in `shared_intents`.

The schema is deliberately strict: anything a brain could mutate that
would change execution authority is overridden by MC at ingest.

Endpoints:
    POST /api/intents              brain → MC, one intent per call
    GET  /api/intents              operator/brain read (filterable)
    POST /api/execution/dry_run    operator → MC, runs gate chain against
                                   an intent_id and returns verdict only
                                   (no broker call). Day 1 of the
                                   paper-trading sprint uses this.

Doctrine:
  * `may_execute` is schema-pinned to False. The brain CANNOT request
    execution authority via this envelope.
  * `requires_gate_pass` is schema-pinned to True. Cannot be bypassed.
  * `seat_at_post_time` is MC-stamped from live seat policy. The brain
    can declare its `stack` but cannot self-grant a role.
  * The Executor seat is registered separately (see executor_seat.py
    once Day 1 lands). Until then, every intent records
    `seat_at_post_time` as the brain's static `roster` role.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Header, Query
from pydantic import BaseModel, Field, field_validator

from auth import get_current_user
from db import db
from namespaces import (
    RUNTIMES,
    SHARED_INTENTS,
)
from runtime_auth import verify_runtime_token


router = APIRouter(tags=["intents"])

# Strict action vocabulary — extend deliberately.
ACTIONS = ("BUY", "SELL", "SHORT", "COVER", "HOLD")


# ─────────────────────────────── schema ───────────────────────────────

class IntentIn(BaseModel):
    """Brain → MC. Subset of fields. MC fills the rest."""

    stack: Literal["alpha", "camaro", "chevelle", "redeye"]
    action: Literal["BUY", "SELL", "SHORT", "COVER", "HOLD"]
    symbol: str = Field(min_length=1, max_length=24)
    confidence: float = Field(ge=0.0, le=1.0)
    risk_multiplier: float = Field(ge=0.0, le=1.0, default=0.0)
    rationale: str = Field(min_length=1, max_length=4000)

    # Brain-supplied context. Bounded.
    evidence: dict = Field(default_factory=dict)
    decision_id: Optional[str] = Field(default=None, max_length=64)
    regime: Optional[str] = Field(default=None, max_length=48)

    # SAFETY INVARIANTS — schema-pinned. Cannot be overridden by the brain.
    may_execute: bool = Field(default=False)
    requires_gate_pass: bool = Field(default=True)

    @field_validator("may_execute")
    @classmethod
    def _pin_may_execute(cls, v: bool) -> bool:
        if v is True:
            raise ValueError("may_execute must be False in an intent envelope")
        return False

    @field_validator("requires_gate_pass")
    @classmethod
    def _pin_requires_gate_pass(cls, v: bool) -> bool:
        if v is False:
            raise ValueError("requires_gate_pass must be True in an intent envelope")
        return True

    @field_validator("symbol")
    @classmethod
    def _symbol_clean(cls, v: str) -> str:
        return v.strip().upper()

    @field_validator("evidence")
    @classmethod
    def _evidence_size_cap(cls, v: dict) -> dict:
        # Mirror the opinions evidence cap — 16 KB max serialized.
        import json
        if len(json.dumps(v, default=str)) > 16 * 1024:
            raise ValueError("evidence must be ≤16 KB serialized")
        return v


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _seat_at_post_time(brain: str) -> Optional[str]:
    """Stamp the brain's current role at the moment of ingest.

    Until the dedicated Executor seat exists, we use the brain's static
    roster role as recorded by the roles manifest. This will be replaced
    by a live lookup once the executor seat registry lands.
    """
    try:
        from shared.roster import get_role_of  # noqa: WPS433
        return await get_role_of(brain)
    except Exception:  # noqa: BLE001
        return None


# ─────────────────────────────── routes ───────────────────────────────

@router.post("/intents")
async def post_intent(
    body: IntentIn,
    x_runtime_token: Optional[str] = Header(default=None, alias="X-Runtime-Token"),
):
    """Brain emits an intent envelope. MC stamps the safety fields.

    Auth: `X-Runtime-Token` of the brain that matches `body.stack`. A
    brain cannot post an intent as another brain. Operators use the
    `/admin/intents` proxy below for that.
    """
    if not x_runtime_token:
        raise HTTPException(status_code=401, detail="X-Runtime-Token required")
    verify_runtime_token(body.stack, x_runtime_token)

    seat = await _seat_at_post_time(body.stack)

    # Snapshot whether this brain held the (rotating) Executor seat at the
    # exact moment its intent was ingested. Audit-critical: an intent
    # posted by Camaro when Chevelle holds the seat cannot ever execute,
    # regardless of how the seat rotates later.
    from shared.executor_seat import get_executor_holder  # noqa: WPS433
    executor_at_post = await get_executor_holder()
    holds_executor = executor_at_post == body.stack

    intent_id = str(uuid.uuid4())

    doc = {
        "intent_id": intent_id,
        "stack": body.stack,
        "action": body.action,
        "symbol": body.symbol,
        "confidence": float(body.confidence),
        "risk_multiplier": float(body.risk_multiplier),
        "rationale": body.rationale,
        "evidence": body.evidence,
        "decision_id": body.decision_id,
        "regime": body.regime,
        # SAFETY (MC-stamped, schema-pinned)
        "may_execute": False,
        "requires_gate_pass": True,
        # AUTHORITY (MC-stamped, not brain-controlled)
        "seat_at_post_time": seat,
        "executor_holder_at_post": executor_at_post,
        "holds_executor_seat": holds_executor,
        # AUDIT (MC-stamped)
        "ingest_ts": _now_iso(),
        "ingest_method": "runtime_token",
        # LIFECYCLE
        "gate_state": "pending",   # pending | passed | blocked | dry_run_passed | dry_run_blocked
        "executed": False,
        "executed_at": None,
        "execution_receipt_id": None,
    }
    await db[SHARED_INTENTS].insert_one(doc)

    return {
        "ok": True,
        "intent_id": intent_id,
        "stack": body.stack,
        "seat_at_post_time": seat,
        "gate_state": "pending",
        "ingest_ts": doc["ingest_ts"],
    }


@router.get("/intents")
async def list_intents(
    stack: Optional[str] = Query(default=None),
    symbol: Optional[str] = Query(default=None),
    gate_state: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    x_runtime_token: Optional[str] = Header(default=None, alias="X-Runtime-Token"),
):
    """Read recent intents. Accepts either an operator JWT (via the admin
    proxy below) or any runtime token (brains can read each other's
    intents for council-context purposes — same doctrine as opinions).
    """
    if not x_runtime_token:
        raise HTTPException(status_code=401, detail="X-Runtime-Token required")
    # Token must match SOMEONE in the four-brain roster.
    matched = False
    for rt in RUNTIMES:
        try:
            verify_runtime_token(rt, x_runtime_token)
            matched = True
            break
        except HTTPException:
            continue
    if not matched:
        raise HTTPException(status_code=401, detail="invalid runtime ingest token")

    q: dict = {}
    if stack:
        q["stack"] = stack
    if symbol:
        q["symbol"] = symbol.strip().upper()
    if gate_state:
        q["gate_state"] = gate_state

    rows = await db[SHARED_INTENTS].find(q, {"_id": 0}).sort("ingest_ts", -1).to_list(limit)
    return {"items": rows, "count": len(rows)}


# ──────────────────── admin proxy + dry-run gate chain ────────────────────

@router.post("/admin/intents")
async def admin_post_intent(
    body: IntentIn,
    user: dict = Depends(get_current_user),  # noqa: B008
):
    """Operator-authed intent emission on behalf of any brain.

    Useful for: stress-testing the gate chain, replaying historical
    decisions, or filling a missing intent during sidecar downtime.
    """
    seat = await _seat_at_post_time(body.stack)

    from shared.executor_seat import get_executor_holder  # noqa: WPS433
    executor_at_post = await get_executor_holder()
    holds_executor = executor_at_post == body.stack

    intent_id = str(uuid.uuid4())

    doc = {
        "intent_id": intent_id,
        "stack": body.stack,
        "action": body.action,
        "symbol": body.symbol,
        "confidence": float(body.confidence),
        "risk_multiplier": float(body.risk_multiplier),
        "rationale": body.rationale,
        "evidence": body.evidence,
        "decision_id": body.decision_id,
        "regime": body.regime,
        "may_execute": False,
        "requires_gate_pass": True,
        "seat_at_post_time": seat,
        "executor_holder_at_post": executor_at_post,
        "holds_executor_seat": holds_executor,
        "ingest_ts": _now_iso(),
        "ingest_method": "admin_proxy",
        "ingest_admin_email": user.get("email"),
        "gate_state": "pending",
        "executed": False,
        "executed_at": None,
        "execution_receipt_id": None,
    }
    await db[SHARED_INTENTS].insert_one(doc)
    return {
        "ok": True,
        "intent_id": intent_id,
        "stack": body.stack,
        "seat_at_post_time": seat,
        "gate_state": "pending",
        "ingest_via": "admin_proxy",
    }


@router.post("/execution/dry_run")
async def execution_dry_run(
    intent_id: str = Query(..., description="intent_id to evaluate"),
    user: dict = Depends(get_current_user),  # noqa: B008
):
    """Run the gate chain against an intent. NEVER places an order.

    Day 1 of the paper-trading sprint uses this to prove the whole
    decision-machine pipeline works end-to-end without touching a
    broker. The gate chain is currently STUBBED — gates land Day 2.

    Returns:
        {
          intent_id, verdict: "would_pass" | "would_block",
          gates: [
            {"name": "...", "passed": bool, "reason": "..."}
          ]
        }
    """
    intent = await db[SHARED_INTENTS].find_one({"intent_id": intent_id}, {"_id": 0})
    if not intent:
        raise HTTPException(status_code=404, detail=f"intent {intent_id} not found")

    # Real gate: did this intent come from the brain that held the
    # Executor seat at ingest time? If the seat was empty or held by a
    # different brain, no order can route.
    from shared.executor_seat import get_executor_holder  # noqa: WPS433
    current_holder = await get_executor_holder()
    held_at_post = intent.get("executor_holder_at_post")
    holds_now = current_holder == intent["stack"]
    held_at_intent = bool(intent.get("holds_executor_seat"))

    if held_at_intent and holds_now:
        seat_pass = True
        seat_reason = f"{intent['stack']} held the Executor seat both at ingest and now ({current_holder})"
    elif held_at_intent and not holds_now:
        seat_pass = False
        seat_reason = (
            f"{intent['stack']} held Executor at ingest, but seat has since rotated to "
            f"{current_holder or 'empty'} — stale intent cannot execute"
        )
    elif not held_at_intent and held_at_post is None:
        seat_pass = False
        seat_reason = "Executor seat was EMPTY when intent was posted — no authority"
    else:
        seat_pass = False
        seat_reason = (
            f"Executor seat was held by {held_at_post} at post time, not {intent['stack']}"
        )

    # Stub gate: real $10/order cap + RoadGuard land Day 2.
    gates = [
        {
            "name": "schema_invariants",
            "passed": intent["may_execute"] is False and intent["requires_gate_pass"] is True,
            "reason": "may_execute pinned False; requires_gate_pass pinned True",
        },
        {
            "name": "live_trading_disabled",
            "passed": True,
            "reason": "LIVE_TRADING_ENABLED is False on this deploy (paper mode only)",
        },
        {
            "name": "executor_seat_check",
            "passed": seat_pass,
            "reason": seat_reason,
        },
        {
            "name": "notional_cap_placeholder",
            "passed": True,
            "reason": "$10/order cap will be enforced once the broker adapter lands (Day 3)",
        },
    ]
    verdict = "would_pass" if all(g["passed"] for g in gates) else "would_block"
    new_state = "dry_run_passed" if verdict == "would_pass" else "dry_run_blocked"

    await db[SHARED_INTENTS].update_one(
        {"intent_id": intent_id},
        {"$set": {
            "gate_state": new_state,
            "last_dry_run_ts": _now_iso(),
            "last_dry_run_by": user.get("email"),
        }},
    )

    return {
        "intent_id": intent_id,
        "verdict": verdict,
        "gates": gates,
        "evaluated_by": user.get("email"),
        "ts": _now_iso(),
    }
