"""Lane Execution Toggles — operator-owned kill switch per lane.

Doctrine pin (2026-02-18):
    Two switches the operator controls directly:
      • `equity` — gates ALL equity-lane order routing
      • `crypto` — gates ALL crypto-lane order routing

    These are DECOUPLED from broker credential state. Credentials can
    stay connected while the toggle is OFF (the gate chain refuses to
    route). The toggle can stay ON while credentials are missing (the
    `broker_connected` gate still refuses). This separation lets the
    operator pause execution without disconnecting accounts.

    Default state: BOTH OFF. Execution is opt-in. Every flip is
    audit-logged with actor + timestamp.

    Gate chain enforcement: `_evaluate_gates` in `shared/execution.py`
    runs a `lane_execution_enabled` gate AFTER `broker_connected`.
    Failure mode: NO_TRADE with a surface reason the operator can read.

Endpoints:
    GET  /api/admin/execution/lane-toggles
    POST /api/admin/execution/lane-toggles   {lane, enabled}
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from auth import get_current_user
from db import db
from namespaces import LANE_EXECUTION_AUDIT_LOG, LANE_EXECUTION_TOGGLES


router = APIRouter(prefix="/admin/execution", tags=["execution"])


_SINGLETON_ID = "current"
LANES = ("equity", "crypto")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ─────────────────────────── core helpers ───────────────────────────


async def get_toggles() -> dict:
    """Return the current toggle doc, materializing safe defaults if
    the row doesn't exist yet. Never returns `_id`."""
    doc = await db[LANE_EXECUTION_TOGGLES].find_one(
        {"_id": _SINGLETON_ID}, {"_id": 0},
    )
    if not doc:
        return {
            "equity": False,
            "crypto": False,
            "created": False,
            "updated_at": None,
            "updated_by": None,
            "equity_updated_at": None,
            "equity_updated_by": None,
            "crypto_updated_at": None,
            "crypto_updated_by": None,
        }
    return {
        "equity": bool(doc.get("equity", False)),
        "crypto": bool(doc.get("crypto", False)),
        "created": True,
        "updated_at": doc.get("updated_at"),
        "updated_by": doc.get("updated_by"),
        "equity_updated_at": doc.get("equity_updated_at"),
        "equity_updated_by": doc.get("equity_updated_by"),
        "crypto_updated_at": doc.get("crypto_updated_at"),
        "crypto_updated_by": doc.get("crypto_updated_by"),
    }


async def is_lane_execution_enabled(lane: str) -> bool:
    """Single source of truth read by the gate chain. Safe default
    when collection is empty: False."""
    lane_norm = (lane or "").lower().strip()
    if lane_norm not in LANES:
        return False
    doc = await db[LANE_EXECUTION_TOGGLES].find_one(
        {"_id": _SINGLETON_ID}, {"_id": 0, lane_norm: 1},
    )
    if not doc:
        return False
    return bool(doc.get(lane_norm, False))


async def set_lane_toggle(lane: str, enabled: bool, actor: str) -> dict:
    """Flip one lane's toggle and audit-log the change."""
    lane_norm = lane.lower().strip()
    if lane_norm not in LANES:
        raise ValueError(f"unknown lane {lane!r}; must be one of {LANES}")
    now = _now_iso()

    # Read previous value for the audit row.
    prev_doc = await db[LANE_EXECUTION_TOGGLES].find_one(
        {"_id": _SINGLETON_ID}, {"_id": 0, lane_norm: 1},
    )
    previous = bool((prev_doc or {}).get(lane_norm, False))

    await db[LANE_EXECUTION_TOGGLES].update_one(
        {"_id": _SINGLETON_ID},
        {
            "$set": {
                lane_norm: bool(enabled),
                f"{lane_norm}_updated_at": now,
                f"{lane_norm}_updated_by": actor,
                "updated_at": now,
                "updated_by": actor,
            },
            "$setOnInsert": {"_id": _SINGLETON_ID, "created_at": now},
        },
        upsert=True,
    )
    # Audit-only insert; never queried by the gate path.
    await db[LANE_EXECUTION_AUDIT_LOG].insert_one({
        "ts": now,
        "lane": lane_norm,
        "previous": previous,
        "next": bool(enabled),
        "actor": actor,
    })
    return await get_toggles()


# ─────────────────────────── routes ───────────────────────────


class ToggleIn(BaseModel):
    lane: Literal["equity", "crypto"]
    enabled: bool = Field(...,
                          description="True = allow routing; False = kill switch")


@router.get("/lane-toggles")
async def list_lane_toggles(_user: dict = Depends(get_current_user)):  # noqa: B008
    """Return the live state of both lane toggles plus doctrine note."""
    toggles = await get_toggles()
    return {
        **toggles,
        "doctrine_note": (
            "Lane execution toggles are the operator's kill switch. "
            "Both default OFF — execution is opt-in. Decoupled from "
            "broker credential state. Enforced by the "
            "`lane_execution_enabled` gate in `_evaluate_gates`."
        ),
    }


@router.post("/lane-toggles")
async def set_lane_toggles(
    body: ToggleIn,
    user: dict = Depends(get_current_user),  # noqa: B008
):
    """Flip one lane's execution toggle. Audit-logged."""
    actor = user.get("email") or "operator"
    try:
        new_state = await set_lane_toggle(body.lane, body.enabled, actor)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {
        "ok": True,
        "lane": body.lane,
        "enabled": body.enabled,
        "actor": actor,
        "state": new_state,
    }


@router.get("/lane-toggles/history")
async def toggle_history(
    limit: int = 50,
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    """Read-only audit trail of toggle flips."""
    rows = (
        await db[LANE_EXECUTION_AUDIT_LOG]
        .find({}, {"_id": 0})
        .sort("ts", -1)
        .to_list(min(max(limit, 1), 500))
    )
    return {"items": rows, "count": len(rows)}
