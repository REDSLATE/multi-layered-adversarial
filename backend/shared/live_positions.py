"""Live Position Lifecycle — open → managing → closed.

Doctrine (2026-02-16):
    A "live position" is a FILLED order tracked from the moment the broker
    accepted it (open) through any size adjustments / partial closes
    (managing) to its final terminal state (closed). This is distinct
    from the discussion-thesis `shared_positions` collection, which
    holds debate primitives (proposed → discussing → consensus_*).

    State machine:
        open      — first fill recorded; position has live notional.
        managing  — any adjustment after the initial fill (scale-in,
                    scale-out, partial close, stop-loss move). Stays in
                    `managing` until fully closed.
        closed    — flat. Broadcast to SHARED_OUTCOMES so the existing
                    outcome scorecards (hit-rate, brier, regime
                    breakdown) pick up the result automatically.

    Storage:
      SHARED_LIVE_POSITIONS       — one doc per position_id (the trade)
      SHARED_LIVE_POSITION_AUDIT  — append-only transition log per pos_id

    Each transition writes a corresponding MC Shelly row (event types
    `position_opened`, `position_managing`, `position_closed`) so the
    operator's training-data substrate captures the trade arc with the
    full roster snapshot at every step.

    Doctrine guards:
      - `may_execute` is never touched here; this module is a recorder.
      - Open is idempotent on (receipt_id) — re-running won't duplicate.
      - Close is one-way; managing → open is forbidden.
      - Close broadcasts a SHARED_OUTCOMES row only ONCE per position.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from auth import get_current_user
from db import db
from namespaces import (
    SHARED_INTENTS,
    SHARED_LIVE_POSITION_AUDIT,
    SHARED_LIVE_POSITIONS,
    SHARED_OUTCOMES,
)


STATE_OPEN = "open"
STATE_MANAGING = "managing"
STATE_CLOSED = "closed"

VALID_STATES = frozenset({STATE_OPEN, STATE_MANAGING, STATE_CLOSED})
TERMINAL_STATES = frozenset({STATE_CLOSED})


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ──────────────────────── helpers ────────────────────────

async def _audit(position_id: str, action: str, actor: str, payload: dict) -> None:
    await db[SHARED_LIVE_POSITION_AUDIT].insert_one({
        "position_id": position_id,
        "ts": _now_iso(),
        "action": action,
        "actor": actor,
        "payload": payload,
    })


def _shelly_event_for_state(state: str) -> str:
    return {
        STATE_OPEN: "position_opened",
        STATE_MANAGING: "position_managing",
        STATE_CLOSED: "position_closed",
    }.get(state, "position_managing")


# ──────────────────────── public service API ────────────────────────

async def open_from_receipt(receipt: dict, intent: Optional[dict] = None) -> Optional[dict]:
    """Idempotent open. Called from `shared/execution.py:execution_submit`
    immediately after a broker accepts the order. If a live position for
    this receipt_id already exists, returns the existing doc (no dup
    insert, no duplicate Shelly write).

    `receipt` shape must match what `execution.py` builds (see comments).
    `intent` is the matching SHARED_INTENTS row (passed in to save a
    second DB hit; if None, we fetch by intent_id).
    """
    receipt_id = receipt.get("receipt_id")
    intent_id = receipt.get("intent_id")
    if not receipt_id or not intent_id:
        return None
    existing = await db[SHARED_LIVE_POSITIONS].find_one(
        {"receipt_id": receipt_id}, {"_id": 0},
    )
    if existing:
        return existing

    if intent is None:
        intent = await db[SHARED_INTENTS].find_one({"intent_id": intent_id}, {"_id": 0}) or {}

    direction: Literal["long", "short"] = "long"
    if (receipt.get("action") in ("SELL", "SHORT")) or (receipt.get("side") == "SELL"):
        direction = "short"

    pos_id = str(uuid.uuid4())
    now = _now_iso()
    fill = {
        "ts": now,
        "kind": "open",
        "broker_order_id": receipt.get("broker_order_id"),
        "notional_usd": float(receipt.get("notional_usd") or 0.0),
        "qty": float(receipt.get("filled_qty") or 0.0),
        "price": receipt.get("filled_avg_price"),
        "status": receipt.get("status"),
    }
    doc = {
        "position_id": pos_id,
        "receipt_id": receipt_id,
        "intent_id": intent_id,
        "stack": receipt.get("stack"),
        "symbol": receipt.get("symbol"),
        "canonical": receipt.get("canonical"),
        "lane": receipt.get("lane"),
        "action": receipt.get("action"),
        "direction": direction,
        "broker": receipt.get("broker"),
        "broker_order_id": receipt.get("broker_order_id"),
        "regime_fp": (intent.get("evidence") or {}).get("regime_fp"),
        "state": STATE_OPEN,
        "opened_at": now,
        "opened_notional_usd": float(receipt.get("notional_usd") or 0.0),
        "current_notional_usd": float(receipt.get("notional_usd") or 0.0),
        "fills": [fill],
        "transitions": [{"from": None, "to": STATE_OPEN, "ts": now, "actor": "system"}],
        "closed_at": None,
        "closed_pnl_usd": None,
        "closed_pnl_pct": None,
        "outcome_label": None,
        "outcome_broadcast_id": None,
        "updated_at": now,
    }
    await db[SHARED_LIVE_POSITIONS].insert_one(doc.copy())
    await _audit(pos_id, "open", "system", {"receipt_id": receipt_id, "fill": fill})

    # MC Shelly — record the open with the full position snapshot.
    try:
        from shared.mc_shelly import record_async  # noqa: WPS433
        record_async(
            event_type="position_opened",
            brain=receipt.get("stack"),
            symbol=receipt.get("symbol"),
            action=receipt.get("action"),
            confidence=intent.get("confidence"),
            regime_fp=(intent.get("evidence") or {}).get("regime_fp"),
            ref_id=pos_id,
            extra={
                "receipt_id": receipt_id,
                "intent_id": intent_id,
                "lane": receipt.get("lane"),
                "direction": direction,
                "notional_usd": fill["notional_usd"],
            },
        )
    except Exception:  # noqa: BLE001
        # Never block the trade on the bookkeeping write.
        pass

    return {k: v for k, v in doc.items() if k != "_id"}


async def record_management(
    *,
    position_id: str,
    actor: str,
    note: str,
    delta_notional_usd: Optional[float] = None,
    new_notional_usd: Optional[float] = None,
    broker_order_id: Optional[str] = None,
    extra: Optional[dict] = None,
) -> dict:
    """Record an in-flight adjustment (scale, partial close, stop move).
    Position transitions to `managing` on first call; subsequent
    adjustments stay in `managing`."""
    pos = await db[SHARED_LIVE_POSITIONS].find_one({"position_id": position_id}, {"_id": 0})
    if not pos:
        raise HTTPException(status_code=404, detail=f"position {position_id} not found")
    if pos["state"] == STATE_CLOSED:
        raise HTTPException(status_code=409, detail=f"position {position_id} is closed")

    now = _now_iso()
    fill = {
        "ts": now,
        "kind": "manage",
        "actor": actor,
        "note": note,
        "delta_notional_usd": delta_notional_usd,
        "broker_order_id": broker_order_id,
        "extra": extra or {},
    }
    current_notional = float(pos.get("current_notional_usd") or 0.0)
    if new_notional_usd is not None:
        current_notional = float(new_notional_usd)
    elif delta_notional_usd is not None:
        current_notional = max(0.0, current_notional + float(delta_notional_usd))

    push: dict = {"fills": fill}
    set_doc: dict = {
        "state": STATE_MANAGING,
        "current_notional_usd": current_notional,
        "updated_at": now,
    }
    if pos["state"] != STATE_MANAGING:
        push["transitions"] = {
            "from": pos["state"], "to": STATE_MANAGING, "ts": now, "actor": actor,
        }
    await db[SHARED_LIVE_POSITIONS].update_one(
        {"position_id": position_id},
        {"$set": set_doc, "$push": push},
    )
    await _audit(position_id, "manage", actor, fill)

    try:
        from shared.mc_shelly import record_async  # noqa: WPS433
        record_async(
            event_type="position_managing",
            brain=pos.get("stack"),
            symbol=pos.get("symbol"),
            action=pos.get("action"),
            regime_fp=pos.get("regime_fp"),
            rationale=note,
            ref_id=position_id,
            extra={"actor": actor, "delta": delta_notional_usd, "new": new_notional_usd},
        )
    except Exception:  # noqa: BLE001
        pass

    return await db[SHARED_LIVE_POSITIONS].find_one({"position_id": position_id}, {"_id": 0})


async def close(
    *,
    position_id: str,
    actor: str,
    pnl_usd: Optional[float] = None,
    pnl_pct: Optional[float] = None,
    outcome_label: Optional[Literal["win", "loss", "scratch", "stopped_out"]] = None,
    note: str = "",
    broker_order_id: Optional[str] = None,
) -> dict:
    """Terminal transition. Idempotent — re-running on an already-closed
    position returns the existing doc without a second outcome broadcast.
    Writes a SHARED_OUTCOMES row so the existing scorecard pipeline picks
    up the result without any extra wiring."""
    pos = await db[SHARED_LIVE_POSITIONS].find_one({"position_id": position_id}, {"_id": 0})
    if not pos:
        raise HTTPException(status_code=404, detail=f"position {position_id} not found")
    if pos["state"] == STATE_CLOSED:
        return pos

    now = _now_iso()
    fill = {
        "ts": now,
        "kind": "close",
        "actor": actor,
        "note": note,
        "broker_order_id": broker_order_id,
        "pnl_usd": pnl_usd,
        "pnl_pct": pnl_pct,
    }
    # Auto-label if the operator didn't supply one.
    label = outcome_label
    if label is None and pnl_usd is not None:
        if pnl_usd > 0:
            label = "win"
        elif pnl_usd < 0:
            label = "loss"
        else:
            label = "scratch"

    # Broadcast to SHARED_OUTCOMES so the existing operator/runtime
    # scorecard pipeline (shared.outcomes._gather_rows) picks up the
    # result. Schema follows OutcomeIn shape (resolved, evidence).
    broadcast_id = str(uuid.uuid4())
    outcome_row = {
        "outcome_id": broadcast_id,
        "source": "live_positions",
        "position_id": position_id,
        "intent_id": pos.get("intent_id"),
        "receipt_id": pos.get("receipt_id"),
        "runtime": pos.get("stack"),
        "stack": pos.get("stack"),
        "symbol": pos.get("symbol"),
        "lane": pos.get("lane"),
        "direction": pos.get("direction"),
        "action": pos.get("action"),
        "outcome_label": label,
        "label": label,                      # alias for legacy readers
        "pnl_usd": pnl_usd,
        "pnl_pct": pnl_pct,
        "opened_at": pos.get("opened_at"),
        "closed_at": now,
        "opened_notional_usd": pos.get("opened_notional_usd"),
        "closed_notional_usd": pos.get("current_notional_usd"),
        "regime_fp": pos.get("regime_fp"),
        "resolved_at": now,
        "resolved_by": actor,
        "evidence": {
            "broker_order_id": broker_order_id,
            "note": note,
        },
    }
    try:
        await db[SHARED_OUTCOMES].insert_one(outcome_row.copy())
    except Exception:  # noqa: BLE001
        # Don't let an outcome write failure prevent the position from
        # closing — the operator can replay outcomes later.
        broadcast_id = None

    await db[SHARED_LIVE_POSITIONS].update_one(
        {"position_id": position_id},
        {
            "$set": {
                "state": STATE_CLOSED,
                "closed_at": now,
                "closed_pnl_usd": pnl_usd,
                "closed_pnl_pct": pnl_pct,
                "outcome_label": label,
                "outcome_broadcast_id": broadcast_id,
                "updated_at": now,
            },
            "$push": {
                "fills": fill,
                "transitions": {
                    "from": pos["state"], "to": STATE_CLOSED, "ts": now, "actor": actor,
                },
            },
        },
    )
    await _audit(position_id, "close", actor, {
        "outcome_label": label,
        "pnl_usd": pnl_usd,
        "pnl_pct": pnl_pct,
        "outcome_broadcast_id": broadcast_id,
    })

    # Doctrine outcome-join: append the close outcome onto the
    # doctrine_sidecars row joined by intent_id so Shelly + the
    # /admin/doctrine/scorecard endpoint can answer "did A_QUALITY win?"
    # without re-aggregating. One-shot, append-only, fail-soft.
    try:
        from shared.doctrine.outcome_join import join_outcome_to_doctrine  # noqa: WPS433
        await join_outcome_to_doctrine(
            intent_id=pos.get("intent_id"),
            position_id=position_id,
            lane=pos.get("lane"),
            symbol=pos.get("symbol"),
            outcome_label=label,
            pnl_usd=pnl_usd,
            pnl_pct=pnl_pct,
            opened_at=pos.get("opened_at"),
            closed_at=now,
            closing_actor=actor,
            extra={
                "stack": pos.get("stack"),
                "direction": pos.get("direction"),
                "outcome_broadcast_id": broadcast_id,
            },
        )
    except Exception:  # noqa: BLE001
        # Outcome-join is advisory — never let it block the position close.
        pass

    try:
        from shared.mc_shelly import record_async  # noqa: WPS433
        record_async(
            event_type="position_closed",
            brain=pos.get("stack"),
            symbol=pos.get("symbol"),
            action=pos.get("action"),
            outcome=label,
            pnl_usd=pnl_usd,
            regime_fp=pos.get("regime_fp"),
            rationale=note,
            ref_id=position_id,
            extra={
                "actor": actor,
                "outcome_broadcast_id": broadcast_id,
                "intent_id": pos.get("intent_id"),
                "receipt_id": pos.get("receipt_id"),
            },
        )
    except Exception:  # noqa: BLE001
        pass

    return await db[SHARED_LIVE_POSITIONS].find_one({"position_id": position_id}, {"_id": 0})


# ──────────────────────── REST surface ────────────────────────

router = APIRouter(prefix="/admin/live-positions", tags=["live_positions"])


class ManageBody(BaseModel):
    note: str = Field(..., min_length=1, max_length=512)
    delta_notional_usd: Optional[float] = Field(default=None)
    new_notional_usd: Optional[float] = Field(default=None, ge=0.0)
    broker_order_id: Optional[str] = Field(default=None, max_length=80)
    extra: Optional[dict] = None


class CloseBody(BaseModel):
    pnl_usd: Optional[float] = Field(default=None)
    pnl_pct: Optional[float] = Field(default=None)
    outcome_label: Optional[Literal["win", "loss", "scratch", "stopped_out"]] = None
    note: str = Field(default="", max_length=512)
    broker_order_id: Optional[str] = Field(default=None, max_length=80)


@router.get("")
async def list_live_positions(
    state: Optional[Literal["open", "managing", "closed"]] = Query(default=None),
    stack: Optional[str] = Query(default=None),
    symbol: Optional[str] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    q: dict = {}
    if state:
        q["state"] = state
    if stack:
        q["stack"] = stack
    if symbol:
        q["symbol"] = symbol.upper()
    rows = await db[SHARED_LIVE_POSITIONS].find(q, {"_id": 0}) \
        .sort("opened_at", -1).to_list(limit)
    open_n = await db[SHARED_LIVE_POSITIONS].count_documents({"state": STATE_OPEN})
    mng_n = await db[SHARED_LIVE_POSITIONS].count_documents({"state": STATE_MANAGING})
    cls_n = await db[SHARED_LIVE_POSITIONS].count_documents({"state": STATE_CLOSED})
    return {
        "items": rows,
        "count": len(rows),
        "totals": {"open": open_n, "managing": mng_n, "closed": cls_n},
    }


@router.get("/{position_id}")
async def get_live_position(
    position_id: str,
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    pos = await db[SHARED_LIVE_POSITIONS].find_one({"position_id": position_id}, {"_id": 0})
    if not pos:
        raise HTTPException(status_code=404, detail=f"position {position_id} not found")
    audit = await db[SHARED_LIVE_POSITION_AUDIT].find(
        {"position_id": position_id}, {"_id": 0},
    ).sort("ts", 1).to_list(200)
    return {"position": pos, "audit": audit}


@router.post("/{position_id}/manage")
async def manage_endpoint(
    position_id: str,
    body: ManageBody,
    user: dict = Depends(get_current_user),  # noqa: B008
):
    return await record_management(
        position_id=position_id,
        actor=user.get("email") or "operator",
        note=body.note,
        delta_notional_usd=body.delta_notional_usd,
        new_notional_usd=body.new_notional_usd,
        broker_order_id=body.broker_order_id,
        extra=body.extra,
    )


@router.post("/{position_id}/close")
async def close_endpoint(
    position_id: str,
    body: CloseBody,
    user: dict = Depends(get_current_user),  # noqa: B008
):
    return await close(
        position_id=position_id,
        actor=user.get("email") or "operator",
        pnl_usd=body.pnl_usd,
        pnl_pct=body.pnl_pct,
        outcome_label=body.outcome_label,
        note=body.note,
        broker_order_id=body.broker_order_id,
    )
