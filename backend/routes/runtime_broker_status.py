"""Broker status for brain sidecars (runtime-token authenticated).

Exposes MC's per-lane broker state — connection, scopes, balance preview,
last fill, last error — so brains can size and skip intents intelligently
WITHOUT holding the actual broker credentials.

Doctrine:
    The brain sees what the broker connection IS, not the keys that
    open it. Keys never leave MC. Status is what we share.

    Pre-existing admin endpoint (`/api/admin/kraken/status`) returns
    the same shape but is JWT-only. This endpoint mirrors it for
    runtime-token holders so brain sidecars can pull on every
    heartbeat without admin access.

    A brain that POSTs an intent for $10,000 of BTC while MC's Kraken
    balance is $40 wastes a gate-chain pass. With this endpoint the
    brain pre-checks buying power and sizes down, or skips, or emits
    a shadow-only intent.

Cache:
    Server-side 10-second cache per-lane. A brain polling on a 30s
    heartbeat hits cache 2-3 times per real broker probe. Lifts the
    floor on Kraken / Alpaca rate-limit pressure.

Auth:
    `X-Runtime-Token` for any of the 4 brains. The endpoint is
    READ-ONLY — no token is asked for which brain wants what, the
    response is identical for all brains. The token just gates
    access (operator can revoke any brain's read by rotating its
    ingest token).
"""
from __future__ import annotations

import os
import time
from typing import Any, Dict, Optional

from fastapi import APIRouter, Header, HTTPException

from db import db
from namespaces import (
    DISCUSSION_PARTICIPANTS,
    EXECUTION_RECEIPTS,
    KRAKEN_CREDENTIALS,
)
from runtime_auth import verify_runtime_token
from shared.lane_execution import is_lane_execution_enabled


router = APIRouter(prefix="/runtime", tags=["broker-status"])


# Cache state — per-lane, 10s TTL. Two-key dict so equity and crypto
# probe independently.
_CACHE_TTL_S = 10.0
_cache: Dict[str, tuple[float, dict]] = {}


def _broker_live_order_enabled() -> bool:
    return os.environ.get("BROKER_LIVE_ORDER_ENABLED", "false").lower() == "true"


async def _last_fill_and_error(lane: str) -> Dict[str, Optional[str]]:
    """Look up the most recent execution_receipt for a lane to surface
    `last_fill_at` and `last_error`. Cheap query: indexed on lane +
    sorted by executed_at desc."""
    out: Dict[str, Optional[str]] = {"last_fill_at": None, "last_error": None}
    fill = await db[EXECUTION_RECEIPTS].find_one(
        {"lane": lane, "status": {"$in": ["filled", "submitted"]}},
        sort=[("executed_at", -1)],
        projection={"_id": 0, "executed_at": 1},
    )
    if fill:
        out["last_fill_at"] = fill.get("executed_at")

    err = await db[EXECUTION_RECEIPTS].find_one(
        {"lane": lane, "status": "rejected"},
        sort=[("executed_at", -1)],
        projection={"_id": 0, "executed_at": 1, "error": 1, "reason": 1},
    )
    if err:
        out["last_error"] = err.get("error") or err.get("reason")
        out["last_error_at"] = err.get("executed_at")
    return out


async def _crypto_status() -> dict:
    """Kraken lane status — derived from KRAKEN_CREDENTIALS singleton
    (already redacted) + lane execution toggle + last fill/error."""
    doc = await db[KRAKEN_CREDENTIALS].find_one(
        {"_id": "singleton"}, {"_id": 0},
    ) or {}
    fills = await _last_fill_and_error("crypto")
    lane_on = await is_lane_execution_enabled("crypto")

    if not doc:
        return {
            "lane": "crypto",
            "connected": False,
            "execution_enabled": False,
            "lane_execution_enabled": lane_on,
            "broker_live_order_enabled": _broker_live_order_enabled(),
            "scopes": {},
            "balance_preview": None,
            "public_key_preview": None,
            **fills,
        }

    return {
        "lane": "crypto",
        "connected": True,
        "execution_enabled": bool(doc.get("execution_enabled", False)),
        "lane_execution_enabled": lane_on,
        "broker_live_order_enabled": _broker_live_order_enabled(),
        # Scope dict already exposes a bool-per-permission shape:
        # {"query_funds": True, "trade": False, ...}
        "scopes": doc.get("scopes", {}),
        # `balance_preview` is the top-3 assets shape from
        # `_summarise_balance` in `shared/crypto/kraken.py`. Asset → str
        # quantity. Safe to expose (tells the brain what's there).
        "balance_preview": doc.get("balance_preview"),
        # 4-char preview only — same shape the admin status returns.
        "public_key_preview": doc.get("public_key_preview"),
        # Operator/timestamp hygiene — useful for "is MC's broker pointed
        # at the right account today" sanity.
        "connected_at": doc.get("created_at"),
        "updated_at": doc.get("updated_at"),
        **fills,
    }


async def _equity_status() -> dict:
    """Equity lane status — derived from the active broker adapter
    (Webull post-Alpaca-deprecation, 2026-02-19) + lane execution
    toggle + last fill/error. The Alpaca-era `account_state` block
    is no longer surfaced because Webull credentials live in env
    vars, not a Mongo singleton — brains size off `last_fill_at`
    and the explicit `connected` flag instead."""
    fills = await _last_fill_and_error("equity")
    lane_on = await is_lane_execution_enabled("equity")
    try:
        from shared.broker_router import adapter_for_lane  # noqa: WPS433
        adapter = await adapter_for_lane("equity")
    except Exception:  # noqa: BLE001
        adapter = None
    connected = adapter is not None
    return {
        "lane": "equity",
        "connected": connected,
        "execution_enabled": connected,
        "lane_execution_enabled": lane_on,
        "broker_live_order_enabled": _broker_live_order_enabled(),
        "scopes": {},
        "account_state": None,
        "public_key_preview": None,
        **fills,
    }


async def _get_lane_cached(lane: str) -> dict:
    now = time.monotonic()
    cached = _cache.get(lane)
    if cached and (now - cached[0]) < _CACHE_TTL_S:
        return cached[1]
    if lane == "crypto":
        data = await _crypto_status()
    elif lane == "equity":
        data = await _equity_status()
    else:
        raise HTTPException(status_code=400, detail=f"unknown lane: {lane}")
    _cache[lane] = (now, data)
    return data


# ─────────────────────────── endpoints ───────────────────────────


@router.get("/broker-status")
async def broker_status_all(
    x_runtime_token: str | None = Header(default=None, alias="X-Runtime-Token"),
) -> dict:
    """Unified broker status — both lanes in one call.

    Auth: any brain's X-Runtime-Token. We don't care which brain is
    asking; the response is the same. We just gate access on token
    validity so a leaked-token rotation kills external reads cleanly.
    """
    # Validate the token belongs to SOMEONE in the participants list.
    if not x_runtime_token:
        raise HTTPException(status_code=401, detail="X-Runtime-Token required")
    matched_brain: Optional[str] = None
    for brain in DISCUSSION_PARTICIPANTS:
        expected = os.environ.get(f"{brain.upper()}_INGEST_TOKEN")
        if expected and x_runtime_token == expected:
            matched_brain = brain
            break
    if not matched_brain:
        raise HTTPException(status_code=401, detail="invalid runtime ingest token")

    crypto = await _get_lane_cached("crypto")
    equity = await _get_lane_cached("equity")
    return {
        "asked_by": matched_brain,
        "cache_ttl_seconds": _CACHE_TTL_S,
        "crypto": crypto,
        "equity": equity,
    }


@router.get("/broker-status/{lane}")
async def broker_status_lane(
    lane: str,
    x_runtime_token: str | None = Header(default=None, alias="X-Runtime-Token"),
) -> dict:
    """Per-lane variant — same payload as the unified endpoint, but
    just one lane. Smaller response when a brain only cares about its
    operating lane (Camaro for crypto, Alpha for equity)."""
    if lane not in {"crypto", "equity"}:
        raise HTTPException(status_code=400, detail=f"lane must be crypto|equity, got {lane!r}")
    if not x_runtime_token:
        raise HTTPException(status_code=401, detail="X-Runtime-Token required")
    matched = False
    for brain in DISCUSSION_PARTICIPANTS:
        expected = os.environ.get(f"{brain.upper()}_INGEST_TOKEN")
        if expected and x_runtime_token == expected:
            matched = True
            break
    if not matched:
        raise HTTPException(status_code=401, detail="invalid runtime ingest token")

    return await _get_lane_cached(lane)
