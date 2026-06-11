"""Broker selection singleton — operator picks per-lane broker.

Operator directive (2026-06-11):
    *"I would like the option to switch between accounts. That way,
    we can build up the trades the stack completes, especially on the
    equity side."*

Persists a single config doc:

  {
    "_id":    "singleton",
    "equity": "public" | "webull",
    "crypto": "kraken" | "webull",
    "updated_at": ISO8601,
    "updated_by": <operator email>,
  }

When the brain emits an intent, it reads this selection and stamps the
chosen broker on `broker_override` so the broker router dispatches
accordingly. Defaults are PRESERVED — equity → Public, crypto → Kraken
— so removing the singleton reverts to lane defaults safely.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from auth import get_current_user
from db import db

router = APIRouter(prefix="/admin/broker-selection", tags=["broker-selection"])

COLLECTION = "broker_selection"
DEFAULT = {"equity": "public", "crypto": "kraken"}

VALID_EQUITY = {"public", "webull"}
VALID_CRYPTO = {"kraken", "webull"}


class BrokerSelectionIn(BaseModel):
    equity: str = Field(default="public")
    crypto: str = Field(default="kraken")


async def get_current_selection() -> Dict[str, str]:
    """Source-of-truth read used by the brain runner + frontend.

    Returns the persisted singleton if present, else the lane
    defaults. Always returns the two-key contract.
    """
    doc = await db[COLLECTION].find_one({"_id": "singleton"})
    if not doc:
        return dict(DEFAULT)
    return {
        "equity": doc.get("equity") or DEFAULT["equity"],
        "crypto": doc.get("crypto") or DEFAULT["crypto"],
    }


@router.get("")
async def read_selection(_user: dict = Depends(get_current_user)) -> Dict[str, Any]:
    sel = await get_current_selection()
    return {
        "selection": sel,
        "available": {
            "equity": sorted(VALID_EQUITY),
            "crypto": sorted(VALID_CRYPTO),
        },
        "defaults": dict(DEFAULT),
    }


@router.put("")
async def update_selection(
    payload: BrokerSelectionIn,
    user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    if payload.equity not in VALID_EQUITY:
        raise HTTPException(
            status_code=400,
            detail=f"equity must be one of {sorted(VALID_EQUITY)}",
        )
    if payload.crypto not in VALID_CRYPTO:
        raise HTTPException(
            status_code=400,
            detail=f"crypto must be one of {sorted(VALID_CRYPTO)}",
        )
    now = datetime.now(timezone.utc).isoformat()
    await db[COLLECTION].update_one(
        {"_id": "singleton"},
        {"$set": {
            "_id": "singleton",
            "equity": payload.equity,
            "crypto": payload.crypto,
            "updated_at": now,
            "updated_by": user.get("email") or user.get("sub") or "operator",
        }},
        upsert=True,
    )
    return {
        "selection": {"equity": payload.equity, "crypto": payload.crypto},
        "updated_at": now,
    }
