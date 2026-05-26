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
