"""Per-pair notional-floor CRUD routes (2026-02-17).

Endpoints (operator JWT):
    GET    /api/admin/kraken/pair-floors             — list all
    PUT    /api/admin/kraken/pair-floors             — bulk upsert
    DELETE /api/admin/kraken/pair-floors/{pair}      — remove a pair

Semantics: see `shared/kraken_pair_floors` doctrine at top of module.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Path
from pydantic import BaseModel, Field, field_validator

from auth import get_current_user
from db import db
from shared.kraken_pair_floors import (
    ALLOWED_POLICIES,
    COLLECTION,
    DEFAULT_MIN_NOTIONAL_USD,
    get_floor,
    invalidate_cache,
)


logger = logging.getLogger("kraken_pair_floors.routes")

router = APIRouter(prefix="/admin/kraken", tags=["kraken-pair-floors"])


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class PairFloorIn(BaseModel):
    pair: str = Field(..., min_length=3, max_length=32)
    min_notional_usd: float = Field(..., ge=0)
    policy: str = "size_up"
    notes: Optional[str] = None

    @field_validator("policy")
    @classmethod
    def _policy_known(cls, v: str) -> str:
        v2 = (v or "").strip().lower()
        if v2 not in ALLOWED_POLICIES:
            raise ValueError(f"policy must be one of {sorted(ALLOWED_POLICIES)}")
        return v2

    @field_validator("pair")
    @classmethod
    def _pair_shape(cls, v: str) -> str:
        v2 = v.strip().upper()
        if "/" not in v2:
            raise ValueError("pair must include a slash (e.g. 'BTC/USD')")
        return v2


class BulkPairFloorsIn(BaseModel):
    floors: list[PairFloorIn]


@router.get("/pair-floors")
async def list_pair_floors(_user: dict = Depends(get_current_user)):
    """List explicit per-pair floors + the effective default for
    unconfigured pairs."""
    rows: list[dict] = []
    async for d in db[COLLECTION].find({}).sort("_id", 1):
        rows.append({
            "pair": d["_id"],
            "min_notional_usd": d.get("min_notional_usd"),
            "policy": d.get("policy", "size_up"),
            "updated_at": d.get("updated_at"),
            "updated_by": d.get("updated_by"),
            "notes": d.get("notes"),
        })
    return {
        "floors": rows,
        "default_min_notional_usd": DEFAULT_MIN_NOTIONAL_USD,
        "default_policy": "size_up",
    }


@router.get("/pair-floors/{pair:path}")
async def get_pair_floor(pair: str = Path(..., description="e.g. BTC/USD"),
                         _user: dict = Depends(get_current_user)):
    """Effective floor for a specific pair — the operator's answer to
    "what will the auto-router do with a tiny order on this pair?"."""
    f = await get_floor(pair.upper())
    return {
        "pair": f.pair,
        "min_notional_usd": f.min_notional_usd,
        "policy": f.policy,
        "is_default": f.is_default,
    }


@router.put("/pair-floors")
async def upsert_pair_floors(body: BulkPairFloorsIn,
                              user: dict = Depends(get_current_user)):
    """Bulk-upsert floors. Overwrites `policy` / `min_notional_usd` /
    `notes` for each supplied pair; existing pairs NOT in the body are
    untouched. To remove a floor entirely, use DELETE."""
    now = _now_iso()
    actor = user.get("email") or "operator"
    written = 0
    for f in body.floors:
        doc = {
            "min_notional_usd": float(f.min_notional_usd),
            "policy": f.policy,
            "updated_at": now,
            "updated_by": actor,
            "notes": f.notes,
        }
        await db[COLLECTION].update_one(
            {"_id": f.pair},
            {"$set": doc},
            upsert=True,
        )
        written += 1
    invalidate_cache()
    logger.info("kraken pair-floors upsert by=%s count=%d", actor, written)
    return {"ok": True, "written": written}


@router.delete("/pair-floors/{pair:path}")
async def delete_pair_floor(pair: str = Path(..., description="e.g. BTC/USD"),
                             user: dict = Depends(get_current_user)):
    """Remove a pair's explicit floor — falls back to the env default
    on the next auto-router tick."""
    p = pair.upper()
    r = await db[COLLECTION].delete_one({"_id": p})
    invalidate_cache()
    if r.deleted_count == 0:
        raise HTTPException(status_code=404, detail=f"no floor configured for {p}")
    logger.info("kraken pair-floor delete by=%s pair=%s", user.get("email"), p)
    return {"ok": True, "deleted": r.deleted_count, "pair": p}
