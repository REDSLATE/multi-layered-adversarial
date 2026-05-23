"""Broker Freeze — emergency kill switch above the lane toggles.

Doctrine pin (2026-05-23):
    On 2026-05-23 the operator surfaced ~500 orphan Alpaca paper fills
    from 2026-05-15 / 2026-05-18 that bypassed MC entirely (Camaro
    sidecar held its own API key and POSTed direct). MC was never
    "trading" — Camaro was, unilaterally.

    The fix has two parts:
      1) The operator-driven audit (this freeze + reconcile + the
         orphan ingester).
      2) Code-level invariants making a bypass impossible going
         forward (adapter-level MC receipt requirement, pre-write
         execution receipts, freeze respected by every broker path).

    The freeze is a SINGLE source of truth checked by EVERY broker
    submit path. It is BOLDER than lane toggles — lane toggles let
    one lane run while another is paused; the freeze blocks ALL
    broker writes regardless of lane, regardless of credentials,
    regardless of gate state. It is the operator's "stop the world"
    button during audits or incidents.

    Defaults to UNFROZEN (off) when the collection is empty so a
    fresh environment behaves like before. The audit-phase row is
    written explicitly via the admin endpoint or the seed script.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from db import db
from namespaces import (
    BROKER_FREEZE_AUDIT_LOG,
    BROKER_FREEZE_STATE,
)


_SINGLETON_ID = "current"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class BrokerFrozen(Exception):
    """Raised when a broker write is attempted while the freeze is on.
    Always NO_TRADE — fail-closed by design."""


async def get_freeze_state() -> dict:
    """Return the current freeze doc, materializing safe defaults if
    the row doesn't exist yet. Never returns `_id`."""
    doc = await db[BROKER_FREEZE_STATE].find_one(
        {"_id": _SINGLETON_ID}, {"_id": 0},
    )
    if not doc:
        return {
            "frozen": False,
            "reason": None,
            "frozen_at": None,
            "frozen_by": None,
            "thawed_at": None,
            "thawed_by": None,
        }
    return {
        "frozen": bool(doc.get("frozen", False)),
        "reason": doc.get("reason"),
        "frozen_at": doc.get("frozen_at"),
        "frozen_by": doc.get("frozen_by"),
        "thawed_at": doc.get("thawed_at"),
        "thawed_by": doc.get("thawed_by"),
    }


async def is_frozen() -> bool:
    """Single-statement read for the broker_router hot path.
    Safe default when collection is empty: False."""
    doc = await db[BROKER_FREEZE_STATE].find_one(
        {"_id": _SINGLETON_ID}, {"_id": 0, "frozen": 1},
    )
    if not doc:
        return False
    return bool(doc.get("frozen", False))


async def freeze(reason: str, actor: str) -> dict:
    """Flip the freeze ON. Audit-logged. Idempotent (overwrites prior
    freeze reason)."""
    now = _now_iso()
    prev = await get_freeze_state()
    await db[BROKER_FREEZE_STATE].update_one(
        {"_id": _SINGLETON_ID},
        {
            "$set": {
                "frozen": True,
                "reason": reason,
                "frozen_at": now,
                "frozen_by": actor,
            },
            "$setOnInsert": {"_id": _SINGLETON_ID, "created_at": now},
        },
        upsert=True,
    )
    await db[BROKER_FREEZE_AUDIT_LOG].insert_one({
        "ts": now,
        "action": "freeze",
        "actor": actor,
        "reason": reason,
        "previous": prev,
    })
    return await get_freeze_state()


async def thaw(actor: str, reason: Optional[str] = None) -> dict:
    """Flip the freeze OFF. Audit-logged. Idempotent."""
    now = _now_iso()
    prev = await get_freeze_state()
    await db[BROKER_FREEZE_STATE].update_one(
        {"_id": _SINGLETON_ID},
        {
            "$set": {
                "frozen": False,
                "thawed_at": now,
                "thawed_by": actor,
                "thaw_reason": reason,
            },
            "$setOnInsert": {"_id": _SINGLETON_ID, "created_at": now},
        },
        upsert=True,
    )
    await db[BROKER_FREEZE_AUDIT_LOG].insert_one({
        "ts": now,
        "action": "thaw",
        "actor": actor,
        "reason": reason,
        "previous": prev,
    })
    return await get_freeze_state()


async def assert_not_frozen() -> None:
    """Raise `BrokerFrozen` if the freeze is on. Called by the broker
    router BEFORE any adapter dispatch. Fail-closed."""
    state = await get_freeze_state()
    if state["frozen"]:
        raise BrokerFrozen(
            f"BROKER FROZEN: {state.get('reason') or 'no reason given'} "
            f"(by {state.get('frozen_by')} at {state.get('frozen_at')}). "
            f"All broker writes are blocked until an operator thaws. "
            f"NO_TRADE."
        )
