"""Per-intent stage trace — the operator's answer to "where exactly
did THIS trade die?"

GET /api/admin/intent-trace/{intent_id}

Stitches together, in pipeline order, every record the system already
keeps about one intent (operator directive 2026-07-20: "failures can
hide between stages" — this makes the seams visible):

    arbiter   — mc_seats decision that minted it (winner, size
                cascade, disagreement, duplicate suppression)
    intent    — the shared_intents contract row (gate_state,
                blocked_by, broker_reason, expiry)
    gates     — shared_gate_results rows, chronological
    broker    — executions submit attempts (ok / broker_status)
    fills     — shared_broker_fills matched by symbol ±30 min of a
                submit (fills carry no intent_id — honest best-effort)

All reads indexed + bounded.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends

from auth import get_current_user
from db import db

router = APIRouter(prefix="/admin/intent-trace", tags=["intent-trace"])

_SEAT_FIELDS = {
    "_id": 0, "seat_key": 1, "brain": 1, "symbol": 1, "lane": 1,
    "direction": 1, "rank_score": 1, "confidence": 1,
    "winner_direction": 1, "size_multiplier": 1,
    "disagreement_multiplier": 1, "opposition_strength": 1,
    "suppressed_duplicate": 1, "suppression_reason": 1,
    "duplicate_of": 1, "emit_error": 1, "arbitrated_at": 1, "field": 1,
}
_INTENT_FIELDS = {
    "_id": 0, "intent_id": 1, "stack": 1, "stack_canonical": 1,
    "symbol": 1, "action": 1, "lane": 1, "confidence": 1,
    "notional_usd": 1, "gate_state": 1, "blocked_by": 1,
    "broker_reason": 1, "broker_error_bucket": 1, "broker_status": 1,
    "broker_order_id": 1, "executed": 1, "executed_at": 1,
    "expire_reason": 1, "expired_at": 1, "ingest_ts": 1,
    "ingest_method": 1, "seat_at_post_time": 1, "may_execute": 1,
    "requires_gate_pass": 1, "size_multiplier": 1,
}


def _parse(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None


def _verdict(intent: Optional[dict], broker_rows: list, fills: list) -> str:
    if intent is None:
        return "NOT FOUND: no shared_intents row — either never emitted (check arbiter suppression) or expired past the 90-day TTL."
    gs = intent.get("gate_state")
    if intent.get("executed") and fills:
        return f"FILLED: executed via {intent.get('broker_status') or 'broker'}; {len(fills)} matched fill(s)."
    if intent.get("executed"):
        return "EXECUTED: broker accepted the order — no fill record matched by symbol/time (fills carry no intent link)."
    if gs == "blocked":
        return (f"DIED AT GATES: blocked by {intent.get('blocked_by') or 'unknown gate'} — "
                f"{intent.get('broker_reason') or '(no reason recorded)'}")
    if intent.get("expire_reason"):
        return (f"DIED UNROUTED: {intent.get('expire_reason')} — the router never picked it up "
                "(master switch off, seat vacant, or router down at the time).")
    if gs == "advisory_only":
        return f"ADVISORY ONLY: downgraded, never routable — {intent.get('broker_reason') or 'seat/authority'}"
    if gs == "rejected_at_ingest":
        return f"DIED AT FIREWALL: rejected at ingest — {intent.get('broker_reason') or 'contract/lane policy'}"
    if broker_rows and not any(r.get("ok") for r in broker_rows):
        last = broker_rows[-1]
        return f"DIED AT BROKER: submit rejected — {last.get('broker_status') or 'unknown'}"
    if gs == "pending":
        return "PENDING: waiting for the auto-router."
    return f"STATE: gate_state={gs}, executed={intent.get('executed')}"


@router.get("/{intent_id}")
async def intent_trace(
    intent_id: str, _user: dict = Depends(get_current_user),
):
    intent = await db["shared_intents"].find_one(
        {"intent_id": intent_id}, _INTENT_FIELDS, max_time_ms=5000,
    )
    seat = await db["mc_seats"].find_one(
        {"intent_id": intent_id}, _SEAT_FIELDS, max_time_ms=5000,
    )
    gates = await db["shared_gate_results"].find(
        {"intent_id": intent_id},
        {"_id": 0, "kind": 1, "reason": 1, "skip_category": 1, "by": 1, "ts": 1},
    ).sort([("ts", 1)]).max_time_ms(5000).to_list(100)
    broker_rows = await db["executions"].find(
        {"intent_id": intent_id},
        {"_id": 0, "ok": 1, "broker": 1, "broker_status": 1,
         "broker_order_id": 1, "qty": 1, "notional_usd": 1, "ts": 1},
    ).sort([("ts", 1)]).max_time_ms(5000).to_list(50)

    # Fills carry no intent_id — match by symbol within ±30 min of a
    # submit attempt (or of executed_at when no executions row exists).
    fills: list[dict] = []
    anchor_ts = None
    if broker_rows:
        anchor_ts = _parse(broker_rows[-1].get("ts"))
    elif intent and intent.get("executed_at"):
        anchor_ts = _parse(intent.get("executed_at"))
    if intent and anchor_ts:
        lo = (anchor_ts - timedelta(minutes=30)).isoformat()
        hi = (anchor_ts + timedelta(minutes=30)).isoformat()
        try:
            fills = await db["shared_broker_fills"].find(
                {"symbol": intent["symbol"], "timestamp": {"$gte": lo, "$lte": hi}},
                {"_id": 0, "raw": 0},
            ).max_time_ms(5000).to_list(20)
        except Exception:  # noqa: BLE001
            fills = []

    # Condense the arbiter field array — full opinions live in mc_seats.
    if seat and isinstance(seat.get("field"), list):
        seat["field"] = [
            {k: r.get(k) for k in ("brain", "direction", "adjusted_rank")}
            for r in seat["field"]
        ]

    return {
        "intent_id": intent_id,
        "found": intent is not None,
        "verdict": _verdict(intent, broker_rows, fills),
        "stage_arbiter": seat,
        "stage_intent": intent,
        "stage_gates": gates,
        "stage_broker": broker_rows,
        "stage_fills": fills,
    }
