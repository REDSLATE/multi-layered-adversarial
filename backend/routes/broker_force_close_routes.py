"""Operator Force-Close — explicit override for closing positions
during a broker freeze.

Doctrine pin (2026-05-23):
    After the bypass audit, the operator's instinct is "close all
    open positions, none of them carry MC receipts anyway." The
    freeze blocks every NORMAL broker write — that's the whole
    point. But the operator must still have a typed, audit-logged
    path to wind down the bypass-origin positions.

    This endpoint provides that path with the following invariants:
      • Every call requires a `reason` string (logged).
      • Every closed position generates an `OPERATOR_FORCED_CLOSE`
        receipt (signed if `RISEDUAL_MC_RECEIPT_SECRET` is set).
      • Every close (success OR failure) writes a row into
        `broker_force_close_log` with the operator email, the
        symbol, qty, side, and the Alpaca response.
      • The freeze stays ON throughout. The override is logged as a
        first-class `FREEZE_OVERRIDE` event so the audit story is
        preserved.
      • Uses Alpaca's per-symbol `DELETE /v2/positions/{symbol}`
        endpoint (not the "close all" bulk DELETE) so we get one
        broker order per symbol with discrete error handling.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from auth import get_current_user
from db import db
from shared.broker_freeze import get_freeze_state
from shared.runtime.platform_survival import MCExecutionReceipt, policy_hash


logger = logging.getLogger(__name__)


router = APIRouter(prefix="/admin/broker", tags=["broker-force-close"])

ALPACA_BASE_URL = "https://paper-api.alpaca.markets"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _resolve_alpaca_creds() -> tuple[str, str]:
    """Reuse the orphan-route resolver (Mongo first, then env)."""
    from routes.alpaca_orphan_routes import _resolve_creds
    return await _resolve_creds()


def _mint_operator_receipt(symbol: str, qty: float, side: str) -> Dict[str, Any]:
    """Mint an operator-forced-close receipt. This is a NEW receipt
    type — not a brain intent receipt. The `reason` field tells the
    settlement oracle this fill is an audit-cleanup, not a strategy
    decision."""
    receipt = MCExecutionReceipt(
        accepted=True,
        final_verdict="OPERATOR_FORCED_CLOSE",
        reason="POST_BYPASS_AUDIT_CLEANUP",
        lane="equity" if not _looks_like_option(symbol) else "options",
        symbol=symbol,
        direction=side.upper(),
        confidence=1.0,
        mc_policy_hash=policy_hash(),
        issued_at_ms=int(datetime.now(timezone.utc).timestamp() * 1000),
    )
    secret = os.environ.get("RISEDUAL_MC_RECEIPT_SECRET", "")
    signed = receipt.sign(secret) if secret else receipt
    from dataclasses import asdict
    return asdict(signed)


def _looks_like_option(symbol: str) -> bool:
    """Cheap heuristic — OSI symbols carry expiry+strike (≥15 chars,
    contains C/P followed by digits)."""
    return len(symbol) >= 15 and any(ch.isdigit() for ch in symbol[-8:])


# ─────────────────────────── core ───────────────────────────


async def _list_positions(api_key: str, secret: str) -> List[Dict[str, Any]]:
    headers = {"APCA-API-KEY-ID": api_key, "APCA-API-SECRET-KEY": secret}
    async with httpx.AsyncClient(timeout=15) as cli:
        r = await cli.get(f"{ALPACA_BASE_URL}/v2/positions", headers=headers)
        r.raise_for_status()
        return r.json()


async def _close_one_position(
    api_key: str, secret: str, symbol: str,
) -> Dict[str, Any]:
    """Call Alpaca's per-symbol close endpoint. Returns the broker
    response or an error dict. Idempotent w.r.t. Alpaca — if the
    position is already closed, returns 4xx."""
    headers = {"APCA-API-KEY-ID": api_key, "APCA-API-SECRET-KEY": secret}
    async with httpx.AsyncClient(timeout=20) as cli:
        r = await cli.delete(
            f"{ALPACA_BASE_URL}/v2/positions/{symbol}",
            headers=headers,
        )
        if r.status_code in (200, 207):
            return {"ok": True, "broker_response": r.json()}
        return {
            "ok": False,
            "status_code": r.status_code,
            "broker_response": r.text[:500],
        }


async def _audit_force_close(
    symbol: str, qty: float, side: str, reason: str, actor: str,
    receipt: Dict[str, Any], broker_result: Dict[str, Any],
    freeze_was_on: bool,
) -> None:
    await db.broker_force_close_log.insert_one({
        "ts": _now_iso(),
        "action": "OPERATOR_FORCED_CLOSE",
        "actor": actor,
        "reason": reason,
        "symbol": symbol,
        "qty": qty,
        "side": side,
        "receipt_signature": receipt.get("signature"),
        "receipt_policy_hash": receipt.get("mc_policy_hash"),
        "broker_ok": broker_result.get("ok"),
        "broker_status_code": broker_result.get("status_code"),
        "broker_response": broker_result.get("broker_response"),
        "freeze_was_on": freeze_was_on,
        "freeze_override": freeze_was_on,
    })


# ─────────────────────────── routes ───────────────────────────


class ForceCloseIn(BaseModel):
    reason: str = Field(..., min_length=3, max_length=400)
    only_symbols: Optional[List[str]] = Field(
        default=None,
        description="If set, close ONLY these symbols. None = all open.",
    )
    stagger_seconds: float = Field(
        default=0.5, ge=0.0, le=10.0,
        description="Delay between submits to spread slippage.",
    )
    confirm: bool = Field(
        default=False,
        description="MUST be true to actually submit closes. False = dry-run.",
    )


@router.post("/force-close-all")
async def force_close_all(
    body: ForceCloseIn,
    user: dict = Depends(get_current_user),  # noqa: B008
):
    """Operator-initiated force close of open positions.

    Bypasses the broker freeze (logged as `FREEZE_OVERRIDE`). Each
    close mints its own `OPERATOR_FORCED_CLOSE` receipt + audit row.
    """
    actor = user.get("email") or "operator"
    freeze_state = await get_freeze_state()
    freeze_was_on = bool(freeze_state.get("frozen"))

    try:
        api_key, api_secret = await _resolve_alpaca_creds()
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=412, detail=f"creds error: {e!r}") from e

    try:
        positions = await _list_positions(api_key, api_secret)
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"alpaca list failed: {e!r}") from e

    if body.only_symbols:
        symset = {s.upper() for s in body.only_symbols}
        positions = [p for p in positions if p["symbol"].upper() in symset]

    if not positions:
        return {
            "ok": True,
            "actor": actor,
            "positions_found": 0,
            "closed": [],
            "failed": [],
            "doctrine_note": "No open positions match. Nothing to do.",
        }

    # Dry-run path — preview only, never touch the broker.
    if not body.confirm:
        return {
            "ok": True,
            "dry_run": True,
            "actor": actor,
            "freeze_was_on": freeze_was_on,
            "positions_found": len(positions),
            "would_close": [
                {
                    "symbol": p["symbol"],
                    "qty": p["qty"],
                    "side": p.get("side"),
                    "market_value": p.get("market_value"),
                    "unrealized_pl": p.get("unrealized_pl"),
                }
                for p in positions
            ],
            "doctrine_note": (
                "Dry-run: nothing submitted. Re-call with confirm=true "
                "to actually close. Each close will be audit-logged "
                "with an OPERATOR_FORCED_CLOSE receipt and "
                f"{'FREEZE_OVERRIDE' if freeze_was_on else 'NORMAL_OPERATION'}."
            ),
        }

    # Live close path.
    closed: List[Dict[str, Any]] = []
    failed: List[Dict[str, Any]] = []

    # If the freeze was on, log the override once up-front.
    if freeze_was_on:
        await db.broker_freeze_audit_log.insert_one({
            "ts": _now_iso(),
            "action": "FREEZE_OVERRIDE",
            "actor": actor,
            "reason": body.reason,
            "previous": freeze_state,
            "scope": "operator_force_close_all",
            "position_count": len(positions),
        })

    for p in positions:
        symbol = p["symbol"]
        qty = float(p.get("qty") or 0)
        side = "sell" if p.get("side") == "long" else "buy"
        receipt = _mint_operator_receipt(symbol, qty, side)

        try:
            result = await _close_one_position(api_key, api_secret, symbol)
        except httpx.HTTPError as e:
            result = {"ok": False, "broker_response": f"http_error: {e!r}"}

        await _audit_force_close(
            symbol=symbol, qty=qty, side=side, reason=body.reason,
            actor=actor, receipt=receipt, broker_result=result,
            freeze_was_on=freeze_was_on,
        )

        if result.get("ok"):
            closed.append({
                "symbol": symbol, "qty": qty, "side": side,
                "broker_response": result.get("broker_response"),
            })
        else:
            failed.append({
                "symbol": symbol, "qty": qty, "side": side,
                "error": result.get("broker_response"),
                "status_code": result.get("status_code"),
            })

        # Stagger to spread out the market impact, especially helpful
        # for thin options legs.
        if body.stagger_seconds:
            await asyncio.sleep(body.stagger_seconds)

    logger.info(
        "operator force-close by %s: reason=%r positions=%d closed=%d failed=%d "
        "freeze_was_on=%s",
        actor, body.reason, len(positions),
        len(closed), len(failed), freeze_was_on,
    )

    return {
        "ok": True,
        "actor": actor,
        "reason": body.reason,
        "freeze_was_on": freeze_was_on,
        "positions_found": len(positions),
        "closed_count": len(closed),
        "failed_count": len(failed),
        "closed": closed,
        "failed": failed,
        "doctrine_note": (
            "Each close audit-logged in broker_force_close_log with the "
            "OPERATOR_FORCED_CLOSE receipt. " +
            ("Freeze was ON throughout — override row written to "
             "broker_freeze_audit_log." if freeze_was_on else
             "Broker was not frozen at the time of these closes.")
        ),
    }


@router.get("/force-close-log")
async def force_close_log(
    limit: int = 50,
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    """Read-only audit trail of every operator force-close."""
    rows = await db.broker_force_close_log.find({}, {"_id": 0}) \
        .sort("ts", -1).to_list(min(max(limit, 1), 500))
    return {"items": rows, "count": len(rows)}
