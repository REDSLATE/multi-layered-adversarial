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
