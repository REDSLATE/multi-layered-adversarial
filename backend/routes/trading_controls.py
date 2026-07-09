"""Trading controls — runtime kill switch and read-only status.

Doctrine pin (2026-05-26):
    The operator MUST be able to halt new broker orders without
    redeploying. The mechanism is a Mongo-backed singleton doc that
    auto-router consults on every tick. Flipping it OFF via the API
    takes effect within `AUTO_ROUTER_INTERVAL_SEC` (default 30s).

    Two independent layers protect order routing:
      1. Env var `AUTO_ROUTER_ENABLED` (deploy-time, requires restart)
      2. Mongo `trading_controls.enabled` (runtime, instant)
    Both must be True for orders to fire. EITHER being False halts.

    Halt is non-destructive: existing positions stay open, broker
    reconciliation keeps running, gates still evaluate. Only the
    final route_order() call is suppressed.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from auth import get_current_user
from db import db


logger = logging.getLogger("risedual.trading_controls")


COLLECTION = "trading_controls"
DOC_ID = "current"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def get_trading_status() -> dict:
    """Return the live trading-controls state. Seeds an OFF default on
    first read — fail-CLOSED: if the doc has never been touched, MC
    refuses to fire orders. Operator must explicitly enable."""
    doc = await db[COLLECTION].find_one({"_id": DOC_ID}, {"_id": 0})
    if doc:
        return doc
    seed = {
        "enabled": False,
        "reason": "first_boot_default_disabled",
        "updated_at": _now_iso(),
        "updated_by": "system_default",
    }
    await db[COLLECTION].update_one(
        {"_id": DOC_ID},
        {"$set": seed, "$setOnInsert": {"created_at": _now_iso()}},
        upsert=True,
    )
    return seed


async def is_trading_enabled() -> bool:
    """Single-line check for the auto-router. Fail-CLOSED on error
    (Mongo unreachable → no orders fire)."""
    try:
        doc = await get_trading_status()
        return bool(doc.get("enabled", False))
    except Exception as exc:  # noqa: BLE001
        logger.warning("trading_controls fail-CLOSED: %s", exc)
        return False


async def set_trading_enabled(
    enabled: bool, reason: str, actor: str,
) -> dict:
    """Flip the runtime switch. Writes an audit row alongside."""
    payload = {
        "enabled": bool(enabled),
        "reason": reason or "(no reason given)",
        "updated_at": _now_iso(),
        "updated_by": actor,
    }
    await db[COLLECTION].update_one(
        {"_id": DOC_ID}, {"$set": payload}, upsert=True,
    )
    # Audit row — append-only.
    await db[f"{COLLECTION}_audit"].insert_one({
        **payload, "ts": _now_iso(),
    })
    return await get_trading_status()


# ─── HTTP surface ───
router = APIRouter(
    prefix="/admin/trading", tags=["trading-controls"],
)


class ToggleIn(BaseModel):
    enabled: bool
    reason: str = Field(default="", max_length=240)


@router.get("/status")
async def status(_user: dict = Depends(get_current_user)) -> dict:
    """Read-only state — UI polls this for the kill-switch indicator."""
    import os
    from shared import sizing_gate
    doc = await get_trading_status()
    return {
        "ok": True,
        "trading_enabled_runtime": bool(doc.get("enabled")),
        "trading_enabled_env": (
            os.environ.get("AUTO_ROUTER_ENABLED", "true").lower() == "true"
        ),
        "trading_will_fire": (
            bool(doc.get("enabled"))
            and os.environ.get("AUTO_ROUTER_ENABLED", "true").lower() == "true"
        ),
        "micro_live_enabled": sizing_gate.MICRO_LIVE_ENABLED,
        "micro_live_default_cap_usd": sizing_gate.MICRO_LIVE_DEFAULT_CAP_USD,
        "micro_live_crypto_cap_usd": sizing_gate.MICRO_LIVE_CRYPTO_CAP_USD,
        "micro_live_equity_cap_usd": sizing_gate.MICRO_LIVE_EQUITY_CAP_USD,
        "reason": doc.get("reason"),
        "updated_at": doc.get("updated_at"),
        "updated_by": doc.get("updated_by"),
    }


@router.post("/toggle")
async def toggle(
    body: ToggleIn, user: dict = Depends(get_current_user),
) -> dict:
    """Flip the kill switch. Both directions require admin auth.

    Going FROM disabled TO enabled requires a reason — that's the
    audit-chain receipt that proves the operator deliberately turned
    trading on rather than it flipping accidentally."""
    actor = (user or {}).get("email") or "operator"
    if body.enabled and not body.reason.strip():
        raise HTTPException(
            status_code=400,
            detail="reason required when enabling trading",
        )
    new_state = await set_trading_enabled(
        body.enabled, body.reason, actor,
    )
    logger.warning(
        "trading_controls FLIPPED: enabled=%s by=%s reason=%r",
        body.enabled, actor, body.reason,
    )
    return {"ok": True, **{k: v for k, v in new_state.items() if k != "_id"}}


@router.get("/audit")
async def audit_log(
    limit: int = 50, _user: dict = Depends(get_current_user),
) -> dict:
    """Last N kill-switch flips. Operator review surface."""
    rows = await db[f"{COLLECTION}_audit"].find(
        {}, {"_id": 0},
    ).sort("ts", -1).to_list(min(limit, 200))
    return {"items": rows, "count": len(rows)}


# ═══════════════════════════════════════════════════════════════════
# UNIFIED ARM SURFACE (2026-07-09 operator directive)
# ═══════════════════════════════════════════════════════════════════
# Doctrine:
#   Production has THREE independent "master" gates and only #1 (an
#   env var) was showing ON, giving the operator false confidence.
#   The two downstream gates (#2 auto_router-path, #3 trader-sidecar)
#   both default to CLOSED when their Mongo docs are missing — which
#   they were. Result: `LIVE / TRUE` badge on the Flags page while
#   every order silently dies at gate #2 or #3.
#
#   This endpoint FLIPS ALL DOWNSTREAM GATES in ONE call so the
#   operator can't accidentally leave the trader half-armed. It
#   writes:
#     • trading_controls._id="current"          (MC path)
#     • runtime_flags._id="master_trading_switch"  (trader path)
#     • ONE unified audit row into trading_controls_audit with
#       source="unified_arm" so both flips share a single receipt.
#
#   Auditability is non-negotiable: every arm/disarm carries actor,
#   reason, ts, and the pre/post state of BOTH docs.

_TRADER_SWITCH_DOC_ID = "master_trading_switch"


class ArmIn(BaseModel):
    enabled: bool
    reason: str = Field(default="", max_length=240)


async def _get_trader_switch_state() -> dict:
    """Read the trader-sidecar arm doc. Doesn't seed on miss — the
    trader sidecar's `state.DEFAULT_MASTER_ARMED` is the source of
    truth for absent-doc semantics, and it's `False`."""
    doc = await db["runtime_flags"].find_one(
        {"_id": _TRADER_SWITCH_DOC_ID},
        {"_id": 0},
    )
    return doc or {"enabled": False, "reason": "no_doc_default_disarmed"}


@router.get("/arm/status")
async def arm_status(_user: dict = Depends(get_current_user)) -> dict:
    """Combined view of ALL three master-switch layers so the operator
    dashboard can render a single truthful indicator.

    Returns:
        env_broker_live      — env `BROKER_LIVE_ORDER_ENABLED`
        env_auto_router      — env `AUTO_ROUTER_ENABLED`
        mc_switch            — `trading_controls.enabled` (auto_router path)
        trader_switch        — `runtime_flags.master_trading_switch.enabled`
        all_armed            — ALL three layers are True
        will_fire            — subset the auto_router actually consults
                                (env_auto_router && mc_switch)
        trader_will_fire     — subset the trader sidecar consults
                                (trader_switch)
    """
    import os
    mc = await get_trading_status()
    tr = await _get_trader_switch_state()
    env_ar = os.environ.get("AUTO_ROUTER_ENABLED", "true").lower() == "true"
    env_bl = os.environ.get("BROKER_LIVE_ORDER_ENABLED", "false").lower() == "true"
    mc_on = bool(mc.get("enabled"))
    tr_on = bool(tr.get("enabled"))

    return {
        "ok": True,
        "env_broker_live": env_bl,
        "env_auto_router": env_ar,
        "mc_switch": {
            "enabled": mc_on,
            "reason": mc.get("reason"),
            "updated_at": mc.get("updated_at"),
            "updated_by": mc.get("updated_by"),
        },
        "trader_switch": {
            "enabled": tr_on,
            "reason": tr.get("reason"),
            "updated_at": tr.get("updated_at"),
            "updated_by": tr.get("updated_by"),
        },
        # Boolean summaries the frontend renders in the arm indicator:
        "will_fire": env_ar and mc_on,
        "trader_will_fire": tr_on,
        "all_armed": env_ar and env_bl and mc_on and tr_on,
    }


@router.post("/arm")
async def arm(
    body: ArmIn, user: dict = Depends(get_current_user),
) -> dict:
    """Unified arm/disarm — flips BOTH downstream Mongo gates in ONE
    call so the trader can't be left half-armed.

    Enabling requires a reason (audit hygiene). Both docs get the
    same reason + timestamp + actor. A single audit row is written
    to `trading_controls_audit` with `source="unified_arm"` so the
    per-doc history + the unified history live in the same table.
    """
    actor = (user or {}).get("email") or "operator"
    if body.enabled and not body.reason.strip():
        raise HTTPException(
            status_code=400,
            detail="reason required when arming trading",
        )

    # Snapshot pre-state for audit traceability.
    mc_pre = await get_trading_status()
    tr_pre = await _get_trader_switch_state()

    # 1. Flip MC-path switch (writes to trading_controls + per-doc audit
    #    row via the existing `set_trading_enabled` helper).
    mc_new = await set_trading_enabled(body.enabled, body.reason, actor)

    # 2. Flip trader-sidecar switch. The `/app/trader` sidecar's
    #    state refresher reads this doc every 60s (or shorter with
    #    `TRADER_CACHE_REFRESH_SEC`), so the arm takes effect within
    #    the next refresh window. `enabled` is the canonical key —
    #    matches `state.py::_master_armed = bool((doc or {}).get("enabled"))`.
    tr_payload = {
        "enabled": bool(body.enabled),
        "reason": body.reason or "(no reason given)",
        "updated_at": _now_iso(),
        "updated_by": actor,
    }
    await db["runtime_flags"].update_one(
        {"_id": _TRADER_SWITCH_DOC_ID},
        {"$set": tr_payload, "$setOnInsert": {"created_at": _now_iso()}},
        upsert=True,
    )

    # 3. Unified audit row — one receipt for both flips. Rides in the
    #    same collection as the per-doc audit so operators can see
    #    the whole flip history in one query.
    await db[f"{COLLECTION}_audit"].insert_one({
        "source": "unified_arm",
        "enabled": bool(body.enabled),
        "reason": body.reason or "(no reason given)",
        "updated_at": _now_iso(),
        "updated_by": actor,
        "ts": _now_iso(),
        "pre_state": {
            "mc_switch": bool(mc_pre.get("enabled")),
            "trader_switch": bool(tr_pre.get("enabled")),
        },
        "post_state": {
            "mc_switch": bool(mc_new.get("enabled")),
            "trader_switch": bool(body.enabled),
        },
    })

    logger.warning(
        "unified_arm FLIPPED: enabled=%s by=%s reason=%r "
        "pre(mc=%s,trader=%s)",
        body.enabled, actor, body.reason,
        bool(mc_pre.get("enabled")), bool(tr_pre.get("enabled")),
    )

    # Return the same shape as `arm/status` so the caller doesn't
    # need a second round-trip to re-render the indicator.
    return await arm_status(_user=user)
