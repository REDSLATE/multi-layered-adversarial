"""Options lane admin — chain feed observability + contract resolution.

GET /api/admin/options/status    lane flag + entitlements + policy
GET /api/admin/options/resolve   live contract resolution dry-run
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Query

from auth import get_current_user
from shared.options.chain import resolve_contract
from shared.risk_sizer.policy import get_sizer_policy, lane_enabled

router = APIRouter(prefix="/admin/options", tags=["options"])


@router.get("/status")
async def options_status(_user: dict = Depends(get_current_user)):  # noqa: B008
    policy = await get_sizer_policy()
    out = {
        "lane_enabled": lane_enabled("options"),
        "policy": policy["options"],
        "entitlements": None,
    }
    try:
        from shared.market_data.webull_quotes import get_quotes_client  # noqa: WPS433
        c = get_quotes_client()
        if c is None:
            out["entitlements"] = {"error": "no Webull quotes client (credentials missing)"}
        else:
            loop = asyncio.get_running_loop()
            ent = await loop.run_in_executor(None, c.get_entitlements)
            out["entitlements"] = {
                "base_subscription": ent.get("base_subscription"),
                "us_option_quotes": (ent.get("data_classes") or {}).get("us_option_quotes"),
            }
    except Exception as exc:  # noqa: BLE001
        out["entitlements"] = {"error": str(exc)}
    return out


@router.get("/resolve")
async def options_resolve(
    underlying: str = Query(..., min_length=1, max_length=6),
    action: str = Query("BUY"),
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    pol = (await get_sizer_policy())["options"]
    return await resolve_contract(underlying.upper(), action.upper(), pol)
