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
    res = await resolve_contract(underlying.upper(), action.upper(), pol)
    # Sizing preview through the REAL engine — no intent_id, so no
    # pending-risk reservation is taken.
    if res.get("contract"):
        try:
            from shared.risk_sizer.sizer import build_position_plan  # noqa: WPS433
            plan = await build_position_plan(
                {"lane": "options", "symbol": underlying.upper(),
                 "action": action.upper(), "option": res["contract"]},
                governor_multiplier=1.0,
                skip_roadguard=True,
            )
            keys = ("approved", "reason", "contracts", "final_notional",
                    "risk_budget", "risk_budget_max", "account_equity",
                    "balance_source", "per_contract_cost",
                    "max_loss_per_contract", "cost_cap")
            res["sizing"] = {k: plan[k] for k in keys if k in plan}
            try:
                from shared.hotpath import policy_snapshot  # noqa: WPS433
                _ps = policy_snapshot.get()
                res["sizing"]["roadguard_clear"] = bool(
                    _ps.get("master_switch_enabled", True)
                    and not _ps.get("broker_freeze_reason"),
                )
            except Exception:  # noqa: BLE001
                pass
        except Exception as exc:  # noqa: BLE001
            res["sizing"] = {"approved": False, "reason": f"preview_error: {exc}"}
    return res
