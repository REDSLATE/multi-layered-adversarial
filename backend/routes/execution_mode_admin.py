"""Entry-mode + promotion-gate admin (2026-08-05 operator directive).

Mode changes to canary/live are REFUSED while the promotion gate is
red unless the operator sends override=true — deployment is earned on
forward-recorded expectancy, not on passing tests.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from auth import get_current_user
from db import db

router = APIRouter(prefix="/admin/entry-mode", tags=["entry-mode"])


@router.get("")
async def entry_mode_get(_user: dict = Depends(get_current_user)):  # noqa: B008
    from shared.execution_mode import (  # noqa: WPS433
        SHADOW_FILLS, get_entry_mode_config,
    )
    shadows = await db[SHADOW_FILLS].find({}, {"_id": 0}).sort(
        "ts", -1).max_time_ms(5000).to_list(10)
    n_shadow = await db[SHADOW_FILLS].count_documents({}, maxTimeMS=5000)
    return {"ok": True, "config": await get_entry_mode_config(),
            "shadow_fills_total": n_shadow, "recent_shadow_fills": shadows}


class ModeBody(BaseModel):
    mode: Optional[Literal["live", "exit_only", "canary"]] = None
    canary_max_trades_per_day: Optional[int] = Field(
        default=None, ge=1, le=50)
    override: bool = False


@router.post("")
async def entry_mode_update(
    body: ModeBody,
    user: dict = Depends(get_current_user),  # noqa: B008
):
    from shared.execution_mode import FLAG_ID, get_entry_mode_config  # noqa: WPS433
    changes = {k: v for k, v in body.model_dump().items()
               if v is not None and k != "override"}
    if body.mode in ("live", "canary") and not body.override:
        from shared.forensics.promotion_gate import gate_status  # noqa: WPS433
        gate = await gate_status()
        if not gate["passed"]:
            raise HTTPException(409, {
                "error": "promotion_gate_not_met",
                "detail": ("automated entries return only after "
                           "forward-recorded expectancy is positive; "
                           "send override=true to force (audited)"),
                "gate": {k: gate[k] for k in ("per_lane", "passed_lanes")},
            })
    if changes:
        changes["updated_at"] = datetime.now(timezone.utc).isoformat()
        changes["updated_by"] = user.get("email") or "operator"
        if body.override:
            changes["override_used"] = True
        await db["runtime_flags"].update_one(
            {"_id": FLAG_ID}, {"$set": changes}, upsert=True)
    return {"ok": True, "config": await get_entry_mode_config()}


@router.get("/promotion-gate")
async def promotion_gate_get(_user: dict = Depends(get_current_user)):  # noqa: B008
    from shared.forensics.promotion_gate import gate_status  # noqa: WPS433
    return {"ok": True, **await gate_status()}


@router.get("/funnel")
async def promotion_funnel(_user: dict = Depends(get_current_user)):  # noqa: B008
    """Shadow-collection funnel diagnostic (2026-08 — 'it's not
    recording the 20 it needs'). Shows every link between intent
    creation and the promotion gate so a starved gate can be traced
    to the exact dead stage. Shadow observations exist ONLY when an
    intent survives all upstream gates and is blocked by exit_only at
    the broker router — a disarmed master switch kills collection."""
    from datetime import timedelta  # noqa: WPS433
    from shared.execution_mode import SHADOW_FILLS, get_entry_mode_config  # noqa: WPS433
    from shared.forensics.promotion_gate import gate_status  # noqa: WPS433
    from shared.risk_sizer.missed_entries import COLLECTION  # noqa: WPS433

    cut = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    try:
        from shared.auto_router import _is_master_switch_armed  # noqa: WPS433
        armed = bool(await _is_master_switch_armed())
    except Exception:  # noqa: BLE001
        armed = None
    mode = (await get_entry_mode_config()).get("mode")

    base = {"action": "BUY", "ingest_ts": {"$gte": cut}}
    n_created = await db["shared_intents"].count_documents(base, maxTimeMS=8000)
    n_master_blocked = await db["shared_intents"].count_documents(
        {**base, "broker_reason": "master_switch_disarmed"}, maxTimeMS=8000)
    n_exit_only = await db["shared_intents"].count_documents(
        {**base, "broker_reason": {"$regex": "exit_only_mode"}}, maxTimeMS=8000)
    n_expired_unrouted = await db["shared_intents"].count_documents(
        {**base, "broker_reason": {"$regex": "^EXPIRED"}}, maxTimeMS=8000)
    # router attempts leave an executions receipt (blocked or filled) —
    # broker_reason alone is unreliable (TTL expiry also writes it)
    n_reached_router = await db["executions"].count_documents(
        {"ts": {"$gte": cut}, "action": "BUY"}, maxTimeMS=8000)
    top_blockers = await db["shared_intents"].aggregate([
        {"$match": {**base, "gate_state": {"$in": ["blocked", "advisory_only",
                                                   "expired_unrouted"]}}},
        {"$group": {"_id": {"$ifNull": ["$broker_reason", "$risk_reason"]},
                    "n": {"$sum": 1}}},
        {"$sort": {"n": -1}}, {"$limit": 8},
    ], maxTimeMS=8000).to_list(8)
    n_shadow = await db[SHADOW_FILLS].count_documents(
        {"ts": {"$gte": cut}, "_id": {"$not": {"$regex": "^shadow-test"}}},
        maxTimeMS=8000)
    n_ledger = await db[COLLECTION].count_documents(
        {"block_reason": {"$regex": "exit_only_mode"}}, maxTimeMS=8000)
    n_scored = await db[COLLECTION].count_documents(
        {"block_reason": {"$regex": "exit_only_mode"},
         "outcome": {"$in": ["tp_hit", "sl_hit", "expired"]}}, maxTimeMS=8000)
    gate = await gate_status()

    if armed is False:
        verdict = ("COLLECTION DEAD: master switch is DISARMED — intents die at "
                   "stage 1 and never reach the router where exit_only records "
                   "shadows. ARM the master switch; exit_only still blocks every "
                   "real entry, so no money can move.")
    elif n_created == 0:
        verdict = "COLLECTION DEAD: no BUY intents created in 7d — brains/pulse not emitting."
    elif n_expired_unrouted >= max(n_created - n_expired_unrouted, 1):
        verdict = ("COLLECTION DEAD: most intents EXPIRE before routing (TTL) — "
                   "the router queue isn't being consumed; master switch or "
                   "pulse arm state is off even if the flag reads armed.")
    elif n_reached_router == 0 and n_exit_only == 0:
        verdict = ("COLLECTION DEAD: intents exist but none reach the broker "
                   "router — upstream gates (seat/risk/timing) kill everything; "
                   "see top_blockers.")
    elif n_exit_only == 0:
        verdict = ("STARVED: intents reach the router but none are blocked by "
                   "exit_only — check execution mode (currently "
                   f"{mode}).")
    elif n_scored == 0:
        verdict = ("STALLED: shadows recorded but none scored yet — missed-entry "
                   "resolver needs bars + the horizon to elapse; if this persists "
                   ">24h check bar coverage for the blocked symbols.")
    else:
        need = gate.get("per_lane", {})
        verdict = (f"FLOWING: {n_scored} scored observations. Per-lane progress: "
                   + ", ".join(f"{k}: {v.get('n', 0)}" for k, v in need.items()))

    return {
        "ok": True, "window_days": 7,
        "master_switch_armed": armed, "execution_mode": mode,
        "funnel": {
            "buy_intents_created": n_created,
            "blocked_master_switch": n_master_blocked,
            "expired_before_routing": n_expired_unrouted,
            "reached_broker_router": n_reached_router,
            "blocked_by_exit_only": n_exit_only,
            "shadow_fills_recorded": n_shadow,
            "ledger_rows_exit_only": n_ledger,
            "scored_observations": n_scored,
        },
        "top_blockers": [{"reason": str(b["_id"])[:120], "n": b["n"]}
                         for b in top_blockers],
        "gate_per_lane": gate.get("per_lane"),
        "verdict": verdict,
    }
