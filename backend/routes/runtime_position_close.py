"""Runtime position-close endpoint (2026-05-24).

Doctrine pin:
    Brains can OPEN a position today by posting `BUY` or `SHORT` to
    `POST /api/intents`. That works — the 12-gate chain routes it.

    Closing is the gap. To close a long, the brain would have to:
      1. Discover its current position size on the broker
      2. Pick the right inverse side (long→SELL, short→COVER)
      3. POST an intent with the correct size

    No brain has clean access to (1). This endpoint exposes a single
    "close my position in {symbol}" verb that:
      * Reads the current broker position via MC's adapter
      * Determines side automatically (long→SELL, short→COVER)
      * Builds an IntentIn and routes it through the SAME 12-gate
        chain as a normal intent
      * Tags the intent with `close_intent=True` so the audit feed
        can distinguish opens from closes

    This is NOT a broker bypass. The intent still passes every gate;
    a frozen lane still blocks the close just like it blocks an open.

Auth: any brain's X-Runtime-Token. The brain that calls is the brain
      recorded as the closer (provenance). A brain may close any
      position MC's broker holds — there is no "this isn't your
      position to close" check, because MC's broker account is shared
      and any seated brain may speak for it.
"""
from __future__ import annotations

import os
from typing import Literal, Optional

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from db import db
from namespaces import DISCUSSION_PARTICIPANTS
from runtime_auth import verify_runtime_token


router = APIRouter(prefix="/runtime/positions", tags=["runtime-positions"])


# ─── helpers ────────────────────────────────────────────────────────


def _resolve_runtime_from_token(token: str) -> Optional[str]:
    for brain in DISCUSSION_PARTICIPANTS:
        expected = os.environ.get(f"{brain.upper()}_INGEST_TOKEN")
        if expected and token == expected:
            return brain
    return None


def _inverse_side(broker_side: str) -> Literal["SELL", "COVER"]:
    """Map broker position side → the action that closes it.

    Alpaca returns side as 'long' / 'short' (lowercase).
    """
    if broker_side.lower() == "long":
        return "SELL"
    if broker_side.lower() == "short":
        return "COVER"
    raise ValueError(f"unknown broker position side: {broker_side!r}")


# ─── request/response ───────────────────────────────────────────────


class CloseIn(BaseModel):
    symbol: str = Field(min_length=1, max_length=24)
    lane: Literal["equity", "crypto"]
    # Partial-close support: fraction ∈ (0, 1.0]. Default 1.0 = full close.
    # 0.5 = close half. <=0 or >1 rejected at the boundary.
    fraction: float = Field(default=1.0, gt=0.0, le=1.0)
    rationale: str = Field(
        default="brain-initiated close",
        min_length=1, max_length=4000,
    )
    # Confidence on this close decision. Defaults high (0.9) because a
    # close intent is a different doctrinal beast — it's not "I think
    # this trade will work" but "I am exiting this position now". The
    # brain can override if it's expressing uncertainty about the exit.
    confidence: float = Field(default=0.9, ge=0.0, le=1.0)


# ─── endpoint ───────────────────────────────────────────────────────


@router.post("/close")
async def close_position(
    body: CloseIn,
    x_runtime_token: str | None = Header(default=None, alias="X-Runtime-Token"),
) -> dict:
    """Close (or partially close) a position via the same gate chain
    as a normal intent.

    Flow:
      1. Authenticate the caller's runtime token.
      2. Look up the current broker position for `symbol` in the lane.
      3. Refuse if there's no position to close.
      4. Determine inverse side: long → SELL, short → COVER.
      5. Compute close qty: current_qty * fraction.
      6. Construct a synthetic IntentIn tagged `close_intent=True`.
      7. Hand off to the existing intent-routing pipeline so the
         12-gate chain runs unchanged.

    Returns the intent_id plus the close metadata.
    """
    if not x_runtime_token:
        raise HTTPException(status_code=401, detail="X-Runtime-Token required")
    closing_brain = _resolve_runtime_from_token(x_runtime_token)
    if not closing_brain:
        raise HTTPException(status_code=401, detail="invalid runtime ingest token")
    # Defensive double-check
    verify_runtime_token(closing_brain, x_runtime_token)

    # ── 1. Discover current position ──────────────────────────────
    position = await _lookup_open_position(body.symbol, body.lane)
    if not position:
        raise HTTPException(
            status_code=404,
            detail=(
                f"no open {body.lane} position for {body.symbol!r}; "
                "nothing to close"
            ),
        )

    # ── 2. Determine inverse side ─────────────────────────────────
    try:
        close_action = _inverse_side(position["side"])
    except ValueError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e

    # ── 3. Compute size ──────────────────────────────────────────
    raw_qty = float(position.get("qty", 0.0))
    if raw_qty <= 0:
        raise HTTPException(
            status_code=400,
            detail=(
                f"position {body.symbol!r} has non-positive qty "
                f"{raw_qty}; refusing to close"
            ),
        )
    close_qty = raw_qty * body.fraction

    # ── 4. Build the intent and route ────────────────────────────
    from shared.intents import IntentIn, post_intent  # noqa: WPS433

    intent_body = IntentIn(
        stack=closing_brain,
        action=close_action,
        symbol=body.symbol,
        lane=body.lane,
        confidence=body.confidence,
        risk_multiplier=1.0 if body.fraction == 1.0 else body.fraction,
        rationale=(
            f"[close_intent fraction={body.fraction}] "
            f"position qty={raw_qty} side={position['side']} "
            f"closing_brain={closing_brain}: {body.rationale}"
        ),
        # Honesty fields — close intents skip the market-judgment
        # versus execution-judgment distinction (closes are always
        # directional, never speculative).
        raw_action=close_action,
        display_action=close_action,
        market_decision=close_action,
        execution_decision="ALLOW",
    )
    # Run through the SAME gate chain as a fresh intent. If a lane
    # freeze or a guard refuses, the close is refused — that's
    # doctrinally correct (operator may freeze to halt all activity).
    result = await post_intent(body=intent_body, x_runtime_token=x_runtime_token)

    # ── 5. Tag the resulting intent with close-intent provenance ──
    intent_id = result.get("intent_id") if isinstance(result, dict) else None
    if intent_id:
        await db["shared_intents"].update_one(
            {"intent_id": intent_id},
            {"$set": {
                "close_intent": True,
                "closing_brain": closing_brain,
                "close_fraction": body.fraction,
                "close_underlying_qty": raw_qty,
                "close_target_qty": close_qty,
                "close_underlying_side": position["side"],
            }},
        )

    return {
        "ok": True,
        "intent_id": intent_id,
        "closing_brain": closing_brain,
        "symbol": body.symbol,
        "lane": body.lane,
        "close_action": close_action,
        "underlying_qty": raw_qty,
        "close_qty": close_qty,
        "underlying_side": position["side"],
        "fraction": body.fraction,
        "routed_through_gate_chain": True,
    }


async def _lookup_open_position(symbol: str, lane: str) -> Optional[dict]:
    """Read the broker's current position for `symbol` in `lane`.

    Equity → Alpaca adapter's `list_positions()`
    Crypto → Kraken balance preview (balance ≠ position; we infer)

    Returns a normalized dict `{symbol, side, qty, avg_entry_price}` or
    None if there's no open position.
    """
    symbol_u = symbol.upper()
    if lane == "equity":
        from shared.broker_router import adapter_for_lane  # noqa: WPS433
        adapter = await adapter_for_lane("equity")
        if not adapter:
            raise HTTPException(
                status_code=503,
                detail="equity broker not connected; cannot read equity position",
            )
        positions = await adapter.list_positions()
        for p in positions or []:
            if p.get("symbol", "").upper() == symbol_u:
                return {
                    "symbol": symbol_u,
                    "side": p.get("side", ""),
                    "qty": float(p.get("qty", 0.0)),
                    "avg_entry_price": p.get("avg_entry_price"),
                }
        return None

    if lane == "crypto":
        # Crypto has no "position" concept at the broker — it's spot
        # balance. We treat any positive base-asset balance as a "long"
        # for close-symmetry purposes. This is a reasonable
        # simplification for the brain-facing API; the gate chain still
        # enforces the per-order $ cap on the SELL it produces.
        from namespaces import KRAKEN_CREDENTIALS  # noqa: WPS433
        doc = await db[KRAKEN_CREDENTIALS].find_one(
            {"_id": "singleton"}, {"_id": 0, "balance_preview": 1},
        ) or {}
        balance = (doc.get("balance_preview") or {}).get(symbol_u)
        if balance is None:
            return None
        try:
            qty = float(balance)
        except (TypeError, ValueError):
            return None
        if qty <= 0:
            return None
        return {"symbol": symbol_u, "side": "long", "qty": qty,
                "avg_entry_price": None}

    raise HTTPException(status_code=400, detail=f"unknown lane: {lane!r}")
