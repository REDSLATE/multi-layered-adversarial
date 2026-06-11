"""Broker selection singleton — operator picks per-lane broker.

Operator directive (2026-06-11):
    *"I would like the option to switch between accounts. That way,
    we can build up the trades the stack completes, especially on the
    equity side."*

Persists a single config doc:

  {
    "_id":    "singleton",
    "equity": "webull",
    "crypto": "kraken" | "webull",
    "updated_at": ISO8601,
    "updated_by": <operator email>,
  }

Operator directive (2026-02-19): Public.com and Alpaca are fully
deprecated. Equity ALWAYS routes through Webull. The doc on production
that still carries `{"equity": "public"}` is silently coerced to
`"webull"` on read so this deploy doesn't trigger a Pydantic validation
crash. Operator sees "Webull" in the UI regardless of stored value.
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
DEFAULT = {"equity": "webull", "crypto": "kraken"}

# Equity is single-broker (Webull) post-Alpaca-and-Public-deprecation.
# Crypto can be Kraken or Webull (Webull as parallel route).
VALID_EQUITY = {"webull"}
VALID_CRYPTO = {"kraken", "webull"}

# Legacy → current broker coercions. Production DB carries historical
# values from before the deprecation (public/alpaca_paper). On READ we
# silently map them to the current default so the API never 500s on
# pre-existing rows; on WRITE the Pydantic schema rejects anything that
# isn't in VALID_*.
_LEGACY_EQUITY_COERCIONS = {"public", "alpaca_paper", "alpaca"}


class BrokerSelectionIn(BaseModel):
    equity: str = Field(default="webull")
    crypto: str = Field(default="kraken")


async def get_current_selection() -> Dict[str, str]:
    """Source-of-truth read used by the brain runner + frontend.

    Returns the persisted singleton if present, else the lane
    defaults. Always returns the two-key contract.

    Silent coercion (2026-02-19): any historical equity value that
    matches `_LEGACY_EQUITY_COERCIONS` is mapped to the current
    default (`webull`). This keeps the production DB record
    `{"equity": "public"}` compatible with the deprecated-broker
    cleanup without an explicit migration step.
    """
    doc = await db[COLLECTION].find_one({"_id": "singleton"})
    if not doc:
        return dict(DEFAULT)
    stored_equity = (doc.get("equity") or "").strip().lower()
    if stored_equity in _LEGACY_EQUITY_COERCIONS or stored_equity not in VALID_EQUITY:
        equity = DEFAULT["equity"]
    else:
        equity = stored_equity
    stored_crypto = (doc.get("crypto") or "").strip().lower()
    crypto = stored_crypto if stored_crypto in VALID_CRYPTO else DEFAULT["crypto"]
    return {"equity": equity, "crypto": crypto}


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
