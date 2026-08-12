"""Moomoo admin — status, connectivity probe, V1 limits, telemetry.
Secrets never leave the environment: this API reports configuration
STATE only (host/port/booleans), never key or password material.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from auth import get_current_user
from db import db

router = APIRouter(prefix="/admin/moomoo", tags=["moomoo"])


@router.get("/status")
async def moomoo_status(_user: dict = Depends(get_current_user)):  # noqa: B008
    from shared.broker.moomoo_adapter import (  # noqa: WPS433
        LIMITS_DEFAULTS, LIMITS_FLAG, configured_summary, get_moomoo_adapter,
    )
    summary = configured_summary()
    limits = {**LIMITS_DEFAULTS,
              **((await db["runtime_flags"].find_one(
                  {"_id": LIMITS_FLAG}, {"_id": 0})) or {})}
    opend = {"reachable": False, "detail": "unconfigured"}
    account = None
    if summary.get("configured"):
        adapter = await get_moomoo_adapter()
        try:
            state = await adapter.md.market_state("AAPL")
            opend = {"reachable": True,
                     "market_state": str(state.get("market_state"))}
            try:
                bp = await adapter.buying_power()
                account = {"buying_power_usd": bp,
                           "trading_env": adapter.cfg["env"]}
            except Exception as exc:  # noqa: BLE001
                account = {"error": str(exc)[:200]}
        except Exception as exc:  # noqa: BLE001
            opend = {"reachable": False, "detail": str(exc)[:200]}
    return {"ok": True, "credentials": summary, "opend": opend,
            "account": account, "limits": limits,
            "deployment_note": (
                "OpenD runs on a machine YOU control (VPS/home), never in "
                "this pod. Point OPEND_HOST/OPEND_PORT at it over a private "
                "network, set MOOMOO_RSA_PRIVATE_KEY_PEM (same key as "
                "OpenD) and MOOMOO_TRADE_PASSWORD_MD5 via Deploy → "
                "Environment Variables. Start with "
                "MOOMOO_TRADING_ENV=SIMULATE.")}


@router.get("/quote/{symbol}")
async def moomoo_quote(symbol: str,
                       _user: dict = Depends(get_current_user)):  # noqa: B008
    from shared.broker.moomoo_adapter import (  # noqa: WPS433
        MoomooMarketDataAdapter, moomoo_config,
    )
    cfg = moomoo_config()
    if not cfg:
        raise HTTPException(409, "moomoo unconfigured — set OPEND_* env vars")
    md = MoomooMarketDataAdapter(cfg)
    try:
        return {"ok": True, "quote": await md.quote(symbol),
                "bid_ask": await md.bid_ask(symbol),
                "fetched_at": datetime.now(timezone.utc).isoformat()}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"moomoo quote failed: {exc}") from exc


class MoomooLimits(BaseModel):
    enabled: Optional[bool] = None
    max_notional_usd: Optional[float] = Field(None, ge=1, le=500)
    one_position_at_a_time: Optional[bool] = None
    rth_only: Optional[bool] = None
    allow_autonomous_options: Optional[bool] = None


@router.post("/limits")
async def moomoo_limits(
    body: MoomooLimits,
    user: dict = Depends(get_current_user),  # noqa: B008
):
    from shared.broker.moomoo_adapter import LIMITS_FLAG  # noqa: WPS433
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(400, "no knobs provided")
    if updates.get("allow_autonomous_options"):
        raise HTTPException(
            409, "autonomous options stay disabled until the options "
                 "execution policy is ready (directive §8)")
    updates["updated_at"] = datetime.now(timezone.utc).isoformat()
    updates["updated_by"] = user.get("email") or "operator"
    await db["runtime_flags"].update_one(
        {"_id": LIMITS_FLAG}, {"$set": updates}, upsert=True)
    doc = await db["runtime_flags"].find_one({"_id": LIMITS_FLAG}, {"_id": 0})
    return {"ok": True, "limits": doc}


@router.get("/telemetry")
async def moomoo_telemetry(_user: dict = Depends(get_current_user)):  # noqa: B008
    from shared.broker_telemetry import recent  # noqa: WPS433
    return {"ok": True, "rows": await recent(limit=25)}
