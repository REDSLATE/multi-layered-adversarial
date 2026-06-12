"""Webull entitlements & live snapshot debug endpoints.

Operator visibility (2026-06-11):
    The operator's brokerage account is Webull Premium + Options L2,
    but Open API quote entitlements are a SEPARATE ledger flipped per
    app-key in the developer portal. This endpoint exposes the
    current state of that ledger and tests each gated endpoint so
    the dashboard can show ✅/❌ per data class instead of relying
    on the operator to manually probe.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException

from auth import get_current_user

logger = logging.getLogger("risedual.webull_admin")
router = APIRouter(prefix="/admin/webull", tags=["webull-admin"])


@router.get("/entitlements")
async def get_entitlements(_user: dict = Depends(get_current_user)) -> Dict[str, Any]:
    """Probe the Webull app key's data entitlements.

    Returns:
      {
        "configured": bool,                # app key + secret present
        "base_subscription": bool,         # any get_app_subscriptions row
        "data_classes": {
          "us_stock_quotes": bool,         # equity NBBO real-time
          "us_option_quotes": bool,        # OPRA real-time
          "us_crypto": bool,               # spot crypto (bundled)
        },
        "subscriptions": [...],            # raw subscription rows
        "stream_capacity": {               # static — from Webull docs
          "max_conns": 5,
          "msg_rate_per_sec": 3,
        },
        "checked_at": float,               # unix ts (cached up to 60s)
      }
    """
    import os
    configured = bool(
        (os.environ.get("WEBULL_APP_KEY") or "").strip()
        and (os.environ.get("WEBULL_APP_SECRET") or "").strip()
    )
    if not configured:
        return {
            "configured": False,
            "base_subscription": False,
            "data_classes": {
                "us_stock_quotes": False,
                "us_option_quotes": False,
                "us_crypto": False,
            },
            "subscriptions": [],
            "stream_capacity": {"max_conns": 5, "msg_rate_per_sec": 3},
            "checked_at": 0.0,
        }

    from shared.market_data.webull_quotes import get_quotes_client  # noqa: WPS433

    client = get_quotes_client()
    if client is None:
        raise HTTPException(
            status_code=503,
            detail="Webull quotes client unavailable — SDK missing or init failed",
        )

    loop = asyncio.get_running_loop()
    ent = await loop.run_in_executor(None, client.get_entitlements)
    out = dict(ent)
    out["configured"] = True
    out["stream_capacity"] = {"max_conns": 5, "msg_rate_per_sec": 3}
    return out


@router.get("/snapshot/{symbol}")
async def get_symbol_snapshot(
    symbol: str, _user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """Operator-facing live snapshot for one ticker. Equity lane.

    Useful for verifying the doctrine enricher pulls the right fields
    against any name in the universe. Returns the enriched payload
    that the brains will see.
    """
    from shared.snapshot_enrich.equity_doctrine import (  # noqa: WPS433
        enrich_equity_doctrine_snapshot,
    )
    base = {"symbol": symbol.upper(), "lane": "equity"}
    enriched = await enrich_equity_doctrine_snapshot(symbol.upper(), base)
    return {"symbol": symbol.upper(), "snapshot": enriched}


# ──────────────── OTOCO bracket — Phase 2 ────────────────────────────

from pydantic import BaseModel, Field  # noqa: E402


class OtocoTestBody(BaseModel):
    """Operator-driven OTOCO test request.

    Doctrine: ATOMIC OTOCO is whole-share integer-qty only. The $1-$10
    small-pilot fractional path stays on `submit_market_order` with
    the passive bracket recorder. This endpoint is for operators to
    fire a real OTOCO on a ticker priced low enough to make whole
    shares affordable (e.g. AAL @ ~$11 → 1 share = $11, just over
    the $10 cap — operator picks tickers).

    Webull's per-order cap is enforced by the SDK / broker; we don't
    re-check here because the OTOCO path is opt-in.
    """
    symbol: str = Field(..., min_length=1, max_length=20)
    qty: int = Field(..., ge=1, le=100, description="integer shares")
    side: str = Field("BUY", pattern="^(BUY|SELL)$")
    target_price: float = Field(..., gt=0.0)
    stop_price: float = Field(..., gt=0.0)
    confirm: str = Field(
        "",
        description="must equal 'execute-otoco' to actually route",
    )


@router.post("/otoco/test")
async def webull_otoco_test(
    body: OtocoTestBody, user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """Fire an atomic OTOCO bracket against Webull.

    Doctrine (P1 Phase 2, 2026-02-19):
      * The MASTER leg is MARKET; TP child is LIMIT at `target_price`;
        SL child is STOP at `stop_price`. Webull manages the lifecycle
        (one fill cancels the other automatically).
      * Sanity-check: backend validates the bracket geometry against
        the live last-trade price before any SDK call.
      * The operator must type 'execute-otoco' in `confirm` — guards
        against accidental clicks.

    This is OPERATOR-DRIVEN. The auto-router still uses the existing
    `submit_market_order` + passive bracket recorder for $1-$10
    fractional intents. Atomic OTOCO is a parallel capability the
    operator can drive directly while we observe how Webull handles
    the combo across fills.
    """
    if body.confirm != "execute-otoco":
        raise HTTPException(
            status_code=400,
            detail=(
                "confirmation phrase missing — set confirm='execute-otoco' "
                "to fire this atomic OTOCO bracket"
            ),
        )

    from shared.broker.webull import get_webull_adapter  # noqa: WPS433
    adapter = await get_webull_adapter()
    if adapter is None:
        raise HTTPException(
            status_code=503,
            detail="Webull adapter not configured (missing credentials?)",
        )

    # Mint a deterministic-ish client_order_id so the operator can
    # reconcile in the Webull UI.
    import uuid as _uuid  # noqa: WPS433
    client_id = f"mc-otoco-{_uuid.uuid4().hex[:10]}"

    try:
        result = await adapter.submit_otoco_market(
            symbol=body.symbol.upper(),
            qty=body.qty,
            side=body.side,
            target_price=body.target_price,
            stop_price=body.stop_price,
            client_order_id=client_id,
            mc_receipt={"signature": f"operator:{user.get('email','?')}"},
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(
            status_code=400,
            detail=f"OTOCO submit failed: {e}",
        ) from e

    return {
        "ok": True,
        "by": user.get("email"),
        "submitted_at": result.get("submitted_at"),
        "combo": {
            "master_broker_order_id": result.get("combo_order_id"),
            "combo_client_order_id": result.get("combo_client_order_id"),
            "tp_client_order_id": result.get("tp_client_order_id"),
            "sl_client_order_id": result.get("sl_client_order_id"),
            "tp_limit_price": result.get("tp_limit_price"),
            "sl_stop_price": result.get("sl_stop_price"),
            "entry_proxy_price": result.get("entry_proxy_price"),
        },
        "order": result,
    }
