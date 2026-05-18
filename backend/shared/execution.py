"""Execution router — intent → gate chain → broker.

Doctrine:
  * Brains never call this router. Operator JWT only.
  * Intent must hold the Executor seat at ingest AND now.
  * Every gate is logged. Block reasons are surfaced to the UI.
  * Caps are SOFTWARE; see `shared/exposure_caps.py`.
  * Order routing uses notional (dollar-amount) market day orders for
    the paper-trading phase — keeps caps trivially enforceable
    regardless of price discovery latency.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from auth import get_current_user
from db import db
from namespaces import (
    EXECUTION_RECEIPTS,
    SHARED_GATE_RESULTS,
    SHARED_INTENTS,
    SHARED_RECEIPTS,
    SOVEREIGN_AUDIT_LOG,
)
from shared.broker.alpaca_routes import get_alpaca_adapter
# Council doctrine and helpers were extracted 2026-02-15 to
# `shared/council.py` to keep this module under control (was 1355
# lines). We import the helpers used by the gate chain and the
# diagnostic endpoint here.
from shared.council import (
    COUNCIL_POLICY,
    _COUNCIL_FRESHNESS_SECONDS,
    _GOVERNOR_OFFLINE_THRESHOLD_SECONDS,
    _authority_call_clause,
    _brain_match_clause,
    _contribution_clause,
    _doc_ts,
    _evaluate_council,
    _governance_verdict,
    _is_fresh,
    _latest_governor_any_call,
    _latest_governor_call,
    _latest_opponent_contribution,
    _normalize_governor_call,
    _policy_for_lane,
    _seat_holder,
)
from shared.exposure_caps import caps_snapshot, evaluate_all
from shared.mc_shelly import record_async


router = APIRouter(tags=["execution"])


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ───────────────────────────── gate chain ─────────────────────────────

async def _evaluate_gates(intent: dict, order_notional_usd: float) -> dict:
    """Run the full gate chain for an intent.

    Returns:
        {
          "verdict": "would_pass" | "would_block",
          "gates": [{name, passed, reason}, ...],
          "order_notional_usd": float,
        }
    """
    gates: list[dict] = []

    # 1. Schema invariants — pinned by IntentIn validators.
    gates.append({
        "name": "schema_invariants",
        "passed": intent.get("may_execute") is False and intent.get("requires_gate_pass") is True,
        "reason": "may_execute pinned False; requires_gate_pass pinned True",
    })

    # 2. Action-routable check — only BUY/SELL/SHORT/COVER are routable.
    action = intent.get("action")
    routable = action in ("BUY", "SELL", "SHORT", "COVER")
    gates.append({
        "name": "action_routable",
        "passed": routable,
        "reason": (
            f"action {action!r} is routable to the broker"
            if routable else
            f"action {action!r} is not a routable order (HOLD/etc are watchlist signals)"
        ),
    })

    # 3. Executor seat — held at ingest AND still held now.
    #    The seat policy is also lane-scoped: a brain holding the equity
    #    `executor` seat cannot fire a crypto intent (and vice versa).
    #    Both checks must pass.
    from shared.seat_policy import seat_may_execute_lane  # noqa: WPS433
    from shared.executor_seat import (  # noqa: WPS433
        get_seat_holder,
        seats_with_execute,
    )
    intent_lane_for_seat = intent.get("lane")
    intent_stack = intent.get("stack")

    # Find any execute-capable seat that's lane-eligible AND currently
    # held by this intent's brain.
    eligible_seats = seats_with_execute(intent_lane_for_seat)
    current_holder = None
    matched_seat = None
    for seat_name in eligible_seats:
        holder = await get_seat_holder(seat_name)
        if holder == intent_stack:
            matched_seat = seat_name
            current_holder = holder
            break
    if current_holder is None:
        # No execute-capable seat for this lane is held by anyone.
        # Do NOT fall back to the equity executor lookup — that would
        # leak equity authority into crypto messaging. Surface the
        # vacancy honestly (2026-02-16 fix).
        current_holder = None

    held_at_intent = bool(intent.get("holds_executor_seat"))
    held_at_post = intent.get("executor_holder_at_post")  # lane-aware as of 2026-02-16
    holds_now = matched_seat is not None
    # Lane-scope check: the matched seat's policy must allow this lane.
    lane_allowed = seat_may_execute_lane(matched_seat, intent_lane_for_seat)

    if holds_now and lane_allowed and held_at_intent:
        seat_pass, seat_reason = True, (
            f"{intent_stack} holds the {matched_seat!r} seat "
            f"(lane={intent_lane_for_seat or 'any'}); held at ingest"
        )
    elif holds_now and not lane_allowed:
        seat_pass, seat_reason = False, (
            f"{intent_stack} holds {matched_seat!r}, but seat does not authorize "
            f"lane={intent_lane_for_seat!r} — wrong-lane seat blocked"
        )
    elif held_at_intent and not holds_now:
        seat_pass, seat_reason = False, (
            f"{intent_stack} held an execute-seat at ingest but no longer "
            f"holds one matching lane={intent_lane_for_seat!r}"
        )
    elif not held_at_intent and held_at_post is None:
        seat_pass, seat_reason = False, (
            f"No execute-seat was held for lane={intent_lane_for_seat!r} when intent was posted "
            f"— seat vacant, no authority"
        )
    else:
        seat_pass, seat_reason = False, (
            f"Execute-seat for lane={intent_lane_for_seat!r} was held by {held_at_post} "
            f"at post time, not {intent_stack}"
        )
    gates.append({"name": "executor_seat_check", "passed": seat_pass, "reason": seat_reason})

    # 4. Live-trading-disabled (DEFANGED 2026-02-17).
    #    This gate used to assert "LIVE_TRADING_ENABLED stays False" and
    #    surface paper-only messaging. Per operator order, all phantom
    #    "blocked" / "paper-only" / "observation-only" enforcements are
    #    removed. The gate is retained for the receipt schema's stability
    #    (downstream consumers still look for the named gate row) but it
    #    is now a no-op pass with a neutral reason.
    gates.append({
        "name": "live_trading_disabled",
        "passed": True,
        "reason": "live order routing enabled — seat policy is the authority",
    })

    # 5. Broker connected — lane-aware. Equity intents need Alpaca;
    #    crypto intents need Kraken. If lane is unknown the resolver
    #    fails closed when routing — surfaced as a separate gate failure.
    intent_lane = intent.get("lane")
    if intent_lane:
        from shared.broker_router import adapter_for_lane as _adapter_for_lane  # noqa: WPS433
        broker_for_intent = await _adapter_for_lane(intent_lane)
        broker_connected = broker_for_intent is not None
        broker_reason = (
            f"broker for lane={intent_lane!r} present ({broker_for_intent.name})"
            if broker_connected else
            f"no broker configured / connected for lane={intent_lane!r}"
        )
    else:
        # Legacy intents without lane fall back to the Alpaca check —
        # this keeps the equities flow alive for any pre-canonical
        # intents already queued in the DB.
        adapter = await get_alpaca_adapter()
        broker_connected = adapter is not None
        broker_reason = (
            "Alpaca paper adapter present (legacy / lane-untagged intent)"
            if broker_connected else
            "lane missing AND Alpaca not connected — NO_TRADE"
        )
    gates.append({
        "name": "broker_connected",
        "passed": broker_connected,
        "reason": broker_reason,
    })

    # ─── 6a. Council enforcement ──────────────────────────────────────
    # Doctrine (rev3, 2026-02-15): SEAT-BOUND graduated verdict. The
    # Governor seat holder's most-recent stance shapes the verdict;
    # only HARD_VETO blocks. Soft dissent down-sizes a strong executor
    # via `risk_multiplier`. See `_evaluate_council` for the policy.
    council_gates, risk_multiplier = await _evaluate_council(intent)
    gates.extend(council_gates)

    # If the council asked for a reduced size, reflect that in the
    # notional that subsequent gates and the broker see. Caps evaluate
    # against the dropped notional so they never accidentally lift
    # under reduced-size trades.
    effective_notional = order_notional_usd * risk_multiplier if risk_multiplier > 0 else order_notional_usd

    # 6b. Hard exposure caps. Lane-aware: crypto gets the $30/order cap;
    #    equities get the lifted global cap.
    side = action or ""
    cap_evals = await evaluate_all(effective_notional, side, lane=intent.get("lane"))
    for c in cap_evals:
        gates.append({"name": c.name, "passed": c.passed, "reason": c.reason})

    verdict = "would_pass" if all(g["passed"] for g in gates) else "would_block"

    # MC Shelly — one row per gate, tagged with intent context. Lets
    # the operator slice training data by "which gate fails most when
    # the OPP is in seat" type questions.
    for g in gates:
        record_async(
            event_type="gate_pass" if g["passed"] else "gate_fail",
            brain=intent.get("stack"),
            symbol=intent.get("symbol"),
            action=intent.get("action"),
            outcome="pass" if g["passed"] else "fail",
            rationale=g.get("reason"),
            ref_id=intent.get("intent_id"),
            gate_name=g.get("name"),
        )

    return {
        "verdict": verdict,
        "gates": gates,
        "order_notional_usd": order_notional_usd,
        "effective_notional_usd": effective_notional,
        "risk_multiplier": risk_multiplier,
        "caps": caps_snapshot(),
    }


# ───────────────────────────── dry-run ─────────────────────────────

@router.post("/execution/dry_run")
async def execution_dry_run(
    intent_id: str = Query(..., description="intent_id to evaluate"),
    order_notional_usd: float = Query(
        default=10.0,
        ge=0.01,
        le=10_000.0,
        description="proposed order notional in USD (defaults to the per-order cap)",
    ),
    user: dict = Depends(get_current_user),  # noqa: B008
):
    """Evaluate the full gate chain WITHOUT placing an order."""
    intent = await db[SHARED_INTENTS].find_one({"intent_id": intent_id}, {"_id": 0})
    if not intent:
        raise HTTPException(status_code=404, detail=f"intent {intent_id} not found")

    result = await _evaluate_gates(intent, order_notional_usd)
    new_state = "dry_run_passed" if result["verdict"] == "would_pass" else "dry_run_blocked"
    await db[SHARED_INTENTS].update_one(
        {"intent_id": intent_id},
        {"$set": {
            "gate_state": new_state,
            "last_dry_run_ts": _now_iso(),
            "last_dry_run_by": user.get("email"),
            "last_dry_run_notional_usd": order_notional_usd,
        }},
    )
    await db[SHARED_GATE_RESULTS].insert_one({
        "intent_id": intent_id,
        "kind": "dry_run",
        "ts": _now_iso(),
        "by": user.get("email"),
        "order_notional_usd": order_notional_usd,
        "verdict": result["verdict"],
        "gates": result["gates"],
    })

    return {
        "intent_id": intent_id,
        "evaluated_by": user.get("email"),
        "ts": _now_iso(),
        **result,
    }


# ───────────────────────────── submit ─────────────────────────────

class SubmitBody(BaseModel):
    intent_id: str = Field(..., min_length=8, max_length=80)
    order_notional_usd: float = Field(default=10.0, ge=0.01, le=10_000.0)
    confirm: str = Field(default="", description="must equal 'execute' to actually route")


@router.post("/execution/submit")
async def execution_submit(
    body: SubmitBody,
    user: dict = Depends(get_current_user),  # noqa: B008
):
    """Route the intent through the gate chain and, if it passes,
    submit a market-day notional order to the broker.

    Idempotency: each intent can be executed AT MOST ONCE. Re-submits
    are rejected with 409.
    """
    if body.confirm != "execute":
        raise HTTPException(
            status_code=400,
            detail="confirmation phrase missing — set confirm='execute' to route this order",
        )

    intent = await db[SHARED_INTENTS].find_one({"intent_id": body.intent_id}, {"_id": 0})
    if not intent:
        raise HTTPException(status_code=404, detail=f"intent {body.intent_id} not found")
    if intent.get("executed"):
        raise HTTPException(
            status_code=409,
            detail=f"intent {body.intent_id} already executed at {intent.get('executed_at')}",
        )

    # Re-run the gate chain at submit time — state may have shifted
    # between the dry-run and the click (seat rotated, caps changed,
    # broker disconnected).
    result = await _evaluate_gates(intent, body.order_notional_usd)
    if result["verdict"] != "would_pass":
        # Audit-log the block so the operator can see why on the page.
        await db[SHARED_GATE_RESULTS].insert_one({
            "intent_id": body.intent_id,
            "kind": "submit_blocked",
            "ts": _now_iso(),
            "by": user.get("email"),
            "order_notional_usd": body.order_notional_usd,
            "verdict": result["verdict"],
            "gates": result["gates"],
        })
        await db[SHARED_INTENTS].update_one(
            {"intent_id": body.intent_id},
            {"$set": {
                "gate_state": "blocked",
                "last_submit_ts": _now_iso(),
                "last_submit_by": user.get("email"),
            }},
        )
        # Pick the first failing gate as the surface reason.
        first_block = next((g for g in result["gates"] if not g["passed"]), None)
        raise HTTPException(
            status_code=403,
            detail={
                "blocked_by": first_block["name"] if first_block else "unknown",
                "reason": first_block["reason"] if first_block else "gate chain blocked",
                "gates": result["gates"],
            },
        )

    # All gates passed — route the order via the broker router (lane-aware).
    side = "BUY" if intent["action"] in ("BUY", "COVER") else "SELL"
    client_order_id = f"mc-{body.intent_id[:8]}-{uuid.uuid4().hex[:6]}"

    try:
        from shared.broker_router import BrokerRouteBlocked as _Blocked  # noqa: WPS433
        from shared.broker_router import route_order as _route_order  # noqa: WPS433
        order = await _route_order(
            intent,
            notional_usd=body.order_notional_usd,
            client_order_id=client_order_id,
        )
    except _Blocked as e:
        await db[SHARED_GATE_RESULTS].insert_one({
            "intent_id": body.intent_id,
            "kind": "submit_no_trade",
            "ts": _now_iso(),
            "by": user.get("email"),
            "reason": str(e),
        })
        record_async(
            event_type="order_rejected",
            brain=intent.get("stack"),
            symbol=intent.get("symbol"),
            action=intent.get("action"),
            outcome="no_trade",
            error_reason=str(e),
            ref_id=body.intent_id,
        )
        raise HTTPException(
            status_code=403,
            detail={"blocked_by": "broker_router", "reason": str(e)},
        ) from e
    except Exception as e:  # noqa: BLE001
        await db[SHARED_GATE_RESULTS].insert_one({
            "intent_id": body.intent_id,
            "kind": "submit_error",
            "ts": _now_iso(),
            "by": user.get("email"),
            "error": str(e),
        })
        record_async(
            event_type="order_rejected",
            brain=intent.get("stack"),
            symbol=intent.get("symbol"),
            action=intent.get("action"),
            outcome="rejected",
            error_reason=str(e),
            ref_id=body.intent_id,
        )
        raise HTTPException(status_code=502, detail=f"broker rejected order: {e}") from e

    now = _now_iso()
    receipt = {
        "receipt_id": str(uuid.uuid4()),
        "intent_id": body.intent_id,
        "stack": intent.get("stack"),
        "symbol": intent.get("symbol"),
        "canonical": order.get("canonical"),
        "lane": order.get("lane"),
        "broker_symbol": order.get("broker_symbol"),
        "action": intent.get("action"),
        "side": side,
        "notional_usd": float(body.order_notional_usd),
        "broker": order.get("broker", "unknown"),
        "broker_order_id": order["order_id"],
        "client_order_id": order.get("client_order_id"),
        "status": order.get("status"),
        "submitted_at": order.get("submitted_at") or now,
        "filled_at": order.get("filled_at"),
        "filled_qty": order.get("filled_qty", 0.0),
        "filled_avg_price": order.get("filled_avg_price"),
        "executed_at": now,
        "executed_by": user.get("email"),
        "gates_passed": result["gates"],
        "mc_receipt": order.get("mc_receipt"),
        "mc_receipt_status": order.get("mc_receipt_status"),
        "mc_receipt_enforced": order.get("mc_receipt_enforced"),
    }
    await db[EXECUTION_RECEIPTS].insert_one(receipt)
    await db[SHARED_INTENTS].update_one(
        {"intent_id": body.intent_id},
        {"$set": {
            "executed": True,
            "executed_at": now,
            "execution_receipt_id": receipt["receipt_id"],
            "broker_order_id": order["order_id"],
            "gate_state": "passed",
            "last_submit_ts": now,
            "last_submit_by": user.get("email"),
        }},
    )
    await db[SHARED_GATE_RESULTS].insert_one({
        "intent_id": body.intent_id,
        "kind": "submit_passed",
        "ts": now,
        "by": user.get("email"),
        "order_notional_usd": float(body.order_notional_usd),
        "broker_order_id": order["order_id"],
        "gates": result["gates"],
    })

    # Live-position lifecycle (2026-02-16) — open a tracked position
    # against this filled receipt. Idempotent on receipt_id; safe if
    # called again. Fire-and-forget would lose the position_id we want
    # to return to the operator, so we await but the call is cheap.
    try:
        from shared.live_positions import open_from_receipt as _open_pos  # noqa: WPS433
        live_pos = await _open_pos(receipt, intent=intent)
    except Exception as e:  # noqa: BLE001
        # Never fail an executed trade on the bookkeeping write.
        print(f"[execution] live_positions.open_from_receipt failed: {e}")
        live_pos = None

    # VRL verification (2026-02-16) — capture slippage/drift evidence
    # immediately. Idempotent on receipt_id. Errors are absorbed; the
    # operator can re-run /api/admin/vrl/verify later if this is skipped.
    try:
        from shared.vrl import verify_receipt as _verify  # noqa: WPS433
        await _verify(receipt, intent=intent)
    except Exception as e:  # noqa: BLE001
        print(f"[execution] vrl.verify_receipt failed: {e}")

    # MC Shelly — record the order routing. Position = EXE by definition
    # (only the executor-seat brain reaches this code path).
    record_async(
        event_type="order_routed",
        brain=intent.get("stack"),
        symbol=intent.get("symbol"),
        action=intent.get("action"),
        outcome="executed",
        ref_id=receipt["receipt_id"],
        extra={
            "broker_order_id": order["order_id"],
            "notional_usd": float(body.order_notional_usd),
            "status": order.get("status"),
        },
    )

    return {
        "ok": True,
        "intent_id": body.intent_id,
        "receipt": receipt,
        "order": order,
        "verdict": "executed",
        "live_position": live_pos,
    }


# ───────────────────────────── receipts ─────────────────────────────

@router.get("/execution/receipts")
async def list_receipts(
    limit: int = Query(default=50, ge=1, le=500),
    intent_id: Optional[str] = Query(default=None),
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    q: dict = {}
    if intent_id:
        q["intent_id"] = intent_id
    rows = (
        await db[EXECUTION_RECEIPTS]
        .find(q, {"_id": 0})
        .sort("executed_at", -1)
        .to_list(limit)
    )
    return {"items": rows, "count": len(rows), "caps": caps_snapshot()}


@router.get("/execution/caps")
async def caps_status(_user: dict = Depends(get_current_user)):  # noqa: B008
    """Operator view of the hard caps + current consumption."""
    from shared.exposure_caps import daily_spend_usd, open_notional_usd  # noqa: WPS433
    spent = await daily_spend_usd()
    open_ = await open_notional_usd()
    caps = caps_snapshot()
    return {
        "caps": caps,
        "today": {
            "spent_usd": spent,
            "remaining_usd": max(0.0, caps["per_day_usd"] - spent),
        },
        "open": {
            "open_notional_usd": open_,
            "remaining_usd": max(0.0, caps["open_notional_usd"] - open_),
        },
    }


@router.get("/config/exposure-caps")
async def exposure_caps_config(_user: dict = Depends(get_current_user)):  # noqa: B008
    """Doctrine surface — single source of truth for exposure caps.
    Pure config, no DB usage. UI, Mission Control, RoadGuard, and future
    clients should all read from this endpoint instead of hardcoding.

    Shape:
        {
          "per_order_usd":        global default per-order cap
          "per_day_usd":          rolling 24h day cap
          "open_notional_usd":    aggregate open-position cap
          "per_order_by_lane_usd": { "<lane>": <cap> }  per-lane overrides
        }

    Effective per-order cap for a given lane:
      per_order_by_lane_usd[lane] if present, else per_order_usd
    """
    return caps_snapshot()




# ──────────────────── council lookup diagnostic ────────────────────
# Operator-facing debug endpoint: shows EXACTLY what the executor's
# seat-bound council gates see for a symbol — who holds Governor /
# Opponent right now, what those occupants last said, and the
# resulting graduated verdict. Use this to verify governance is being
# heard before deploying changes.

@router.get("/admin/council/lookup-debug")
async def council_lookup_debug(
    symbol: str = Query(..., min_length=1, max_length=32),
    executor_confidence: float = Query(
        default=0.7, ge=0.0, le=1.0,
        description="simulated executor conviction to test the verdict against",
    ),
    action: str = Query(default="BUY", description="simulated intent action"),
    lane: str = Query(default="equity", description="equity or crypto"),
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    """Returns who holds each seat, what they last said, and the
    graduated verdict that would fire for a hypothetical intent at
    `executor_confidence` on the requested `lane`. This makes seat-
    binding and lane-policy visible: switch the Governor seat or the
    lane and re-hit this endpoint to see the verdict flip."""
    policy = _policy_for_lane(lane)
    governor_holder, gov_doc = await _latest_governor_call(symbol, lane=lane)
    _, gov_any = await _latest_governor_any_call(lane=lane)
    opponent_holder, opp_doc = await _latest_opponent_contribution(lane=lane)
    executor_holder = await _seat_holder("executor", lane=lane)
    gov_norm = _normalize_governor_call(gov_doc)
    gov_any_ts = _doc_ts(gov_any)
    governor_alive = _is_fresh(gov_any_ts, _GOVERNOR_OFFLINE_THRESHOLD_SECONDS)

    # Compute the verdict a real intent would receive.
    sim_intent = {
        "intent_id": "diagnostic-sim",
        "symbol": symbol,
        "action": action.upper(),
        "confidence": executor_confidence,
        "stack": executor_holder,
        "lane": lane,
    }
    verdict = _governance_verdict(sim_intent, gov_norm, governor_alive, governor_holder, policy)

    # Collection health: counts under the CURRENT seat occupants.
    gov_total = 0
    if governor_holder:
        gov_total = await db[SHARED_RECEIPTS].count_documents(
            {"$and": [_brain_match_clause(governor_holder), _authority_call_clause()]}
        )
    opp_total = 0
    if opponent_holder:
        opp_total = await db[SOVEREIGN_AUDIT_LOG].count_documents(
            {"$and": [_brain_match_clause(opponent_holder), _contribution_clause()]}
        )

    return {
        "symbol": symbol,
        "lane": lane,
        "policy_used": "crypto" if lane.lower() == "crypto" else "equity",
        "seats": {
            "executor": executor_holder,
            "governor": governor_holder,
            "opponent": opponent_holder,
        },
        "collection_health": {
            "shared_receipts_collection": SHARED_RECEIPTS,
            "governor_authority_call_total": gov_total,
            "sovereign_audit_collection": SOVEREIGN_AUDIT_LOG,
            "opponent_entries_total": opp_total,
        },
        "governor": {
            "holder": governor_holder,
            "call_found_for_symbol": gov_doc is not None,
            "normalized": gov_norm,
            "raw_doc": gov_doc,
            "any_recent_call_ts": gov_any_ts,
            "governor_alive": governor_alive,
            "governor_offline_threshold_seconds": _GOVERNOR_OFFLINE_THRESHOLD_SECONDS,
        },
        "opponent": {
            "holder": opponent_holder,
            "doc_found": opp_doc is not None,
            "doc_ts": _doc_ts(opp_doc),
            "fresh": _is_fresh(_doc_ts(opp_doc)),
            "raw_doc": opp_doc,
            "freshness_window_seconds": _COUNCIL_FRESHNESS_SECONDS,
        },
        "simulated_verdict": {
            "input_executor_confidence": executor_confidence,
            "input_action": action.upper(),
            "input_lane": lane,
            **verdict,
        },
        "active_policy": policy,
        "all_policies": COUNCIL_POLICY,
    }



# ──────────────────── live-trade gate diagnose ────────────────────
# Operator-facing diagnose endpoint. Surfaces ALL blockers preventing
# a live trade on a given lane WITHOUT requiring an actual intent.
# Use when "no trades are being made" to see exactly which gate is
# stopping the order. Also runs broker-adapter sanity (Kraken keys
# decrypt, Alpaca adapter loads, etc.).

@router.get("/admin/execution/diagnose")
async def execution_diagnose(
    lane: str = Query(default="crypto", description="equity or crypto"),
    notional_usd: float = Query(default=25.0, gt=0.0, le=100_000.0),
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    """Run the full gate chain against a synthetic intent for `lane` and
    return every gate's pass/fail plus broker-adapter sanity. The
    response surfaces the FIRST blocker so the operator can act."""
    from shared.broker_router import adapter_for_lane as _adapter_for_lane  # noqa: WPS433
    from shared.crypto.kraken import get_active_keys_status  # noqa: WPS433
    from shared.broker.alpaca_routes import get_alpaca_adapter  # noqa: WPS433
    from shared.executor_seat import get_seat_holder, seats_with_execute  # noqa: WPS433

    lane_l = (lane or "crypto").lower()
    if lane_l not in ("equity", "crypto"):
        raise HTTPException(status_code=400, detail=f"lane must be equity|crypto, got {lane!r}")

    # Symbol pick — first one the user has caps for. Crypto: BTC/USD;
    # equity: SPY. Either works as a probe.
    sample_symbol = "BTC/USD" if lane_l == "crypto" else "SPY"

    # Find current executor seat holder for this lane (so the synthetic
    # intent's `stack` matches whoever owns the seat — otherwise the
    # gate would always fail on seat-mismatch and obscure other issues).
    executor_holder = None
    for s in seats_with_execute(lane_l):
        h = await get_seat_holder(s)
        if h:
            executor_holder = h
            break

    sim_intent = {
        "intent_id": "diagnose-sim",
        "stack": executor_holder or "operator",
        "symbol": sample_symbol,
        "action": "BUY",
        "lane": lane_l,
        "may_execute": False,
        "requires_gate_pass": True,
        "holds_executor_seat": executor_holder is not None,
        "executor_holder_at_post": executor_holder,
        "confidence": 0.7,
    }
    gate_result = await _evaluate_gates(sim_intent, notional_usd)

    # Broker-adapter sanity.
    broker_status: dict = {"lane": lane_l}
    if lane_l == "crypto":
        kraken_status = await get_active_keys_status()
        broker_status["kraken_credentials"] = {
            k: v for k, v in kraken_status.items()
            if k not in ("public_key", "private_key")  # never leak plaintext
        }
        # Operator-facing remediation hint keyed by failure state.
        REMEDIATION = {
            "ok": "credentials decrypted — if orders still fail, check API key scopes (must include `execute_orders` and `query_funds`) on kraken.com",
            "no_credentials": "POST {public_key, private_key} to /api/admin/kraken/connect to seed the encrypted singleton",
            "missing_field": "singleton exists but a field is empty — re-POST both keys to /api/admin/kraken/connect to overwrite",
            "decrypt_failed": "CREDENTIALS_ENCRYPTION_KEY drifted vs encrypt-time. Re-POST both keys to /api/admin/kraken/connect to re-encrypt under the current key.",
        }
        broker_status["remediation"] = REMEDIATION.get(
            kraken_status.get("state"), "see kraken_credentials.detail",
        )
        adapter = await _adapter_for_lane("crypto")
        broker_status["adapter_loaded"] = adapter is not None
        broker_status["adapter_name"] = getattr(adapter, "name", None)
    else:
        adapter = await get_alpaca_adapter()
        broker_status["adapter_loaded"] = adapter is not None
        broker_status["adapter_name"] = getattr(adapter, "name", None)
        # Alpaca status doc preview (no secrets).
        doc = await db["alpaca_credentials"].find_one(
            {"_id": "singleton"},
            {"_id": 0, "execution_enabled": 1, "paper": 1, "key_id_preview": 1, "updated_at": 1},
        )
        broker_status["alpaca_credentials"] = doc
        broker_status["remediation"] = (
            "POST {key_id, secret_key, paper} to /api/admin/alpaca/connect "
            "if alpaca_credentials is None or execution_enabled=False."
        ) if not doc or not doc.get("execution_enabled") else "alpaca connection live"

    first_block = next((g for g in gate_result["gates"] if not g["passed"]), None)

    return {
        "lane": lane_l,
        "sample_symbol": sample_symbol,
        "synthetic_notional_usd": notional_usd,
        "synthetic_intent": sim_intent,
        "verdict": gate_result["verdict"],
        "first_blocker": first_block,
        "gates": gate_result["gates"],
        "broker": broker_status,
        "caps": gate_result.get("caps"),
        "risk_multiplier": gate_result.get("risk_multiplier"),
        "checked_at": _now_iso(),
    }
