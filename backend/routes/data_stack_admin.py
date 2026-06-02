"""Data Stack Phase 1 — operator admin routes (2026-05-27).

Endpoints:
  GET    /api/admin/feeders/health-audit            per-provider error log + roll-up
  GET    /api/admin/patterns/universe               watchlist
  POST   /api/admin/patterns/universe               add symbol
  DELETE /api/admin/patterns/universe/{symbol}      remove symbol
  GET    /api/admin/symbol-metadata                 list cached symbol metadata
  GET    /api/admin/alt-data/filings                Form-4 index (descriptive)
  GET    /api/admin/alt-data/macro                  cached FRED series observations

Doctrine pin: all writes here are DESCRIPTIVE. Adding a symbol to the
universe means MC will scan it; it does NOT grant execution authority
or modify any seat assignment.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

from auth import get_current_user
from db import db
from namespaces import (
    ALT_DATA_FILINGS,
    ALT_DATA_MACRO,
    FEEDER_HEALTH_AUDIT,
    PATTERNS_UNIVERSE,
    SYMBOL_METADATA,
)


router = APIRouter(tags=["data_stack_phase1"])


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ──────────────────────── feeder health audit ────────────────────────

@router.get("/admin/feeders/health-audit")
async def feeder_health_audit(
    provider: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=1000),
    _user: dict = Depends(get_current_user),
):
    """Roll-up of recent feeder errors / rate-limit hits.

    Doctrine: descriptive observability — failed fetches NEVER block
    ingest. This endpoint surfaces what's failing so the operator can
    investigate.
    """
    q: dict = {}
    if provider:
        q["provider"] = provider
    rows = await db[FEEDER_HEALTH_AUDIT].find(q, {"_id": 0}).sort(
        "ts", -1,
    ).to_list(limit)
    # Per-provider counts in the recent window
    pipeline = [
        {"$group": {
            "_id": {"provider": "$provider", "error_type": "$error_type"},
            "count": {"$sum": 1},
            "last_ts": {"$max": "$ts"},
        }},
        {"$project": {
            "_id": 0,
            "provider": "$_id.provider",
            "error_type": "$_id.error_type",
            "count": 1,
            "last_ts": 1,
        }},
        {"$sort": {"last_ts": -1}},
    ]
    summary = await db[FEEDER_HEALTH_AUDIT].aggregate(pipeline).to_list(200)
    return {
        "items": rows, "count": len(rows),
        "summary": summary,
        "doctrine": (
            "Feeder health audit is observability only. Failed fetches "
            "never block ingest; the pipeline degrades gracefully."
        ),
    }


# ──────────────────────── patterns universe (watchlist) ────────────────────────

class UniverseSymbolIn(BaseModel):
    symbol: str = Field(..., min_length=1, max_length=32)
    note: Optional[str] = Field(None, max_length=500)
    active: bool = True
    lane: str = Field(
        default="equity",
        description=(
            "Which lane this symbol belongs to. Brains may only "
            "propose intents for symbols in their lane's universe. "
            "Defaults to 'equity' for backward compat — every legacy "
            "row without a `lane` field is treated as equity."
        ),
    )

    @field_validator("symbol")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.upper().strip()

    @field_validator("lane")
    @classmethod
    def _lane_canonical(cls, v: str) -> str:
        v2 = (v or "").lower().strip()
        if v2 not in ("equity", "crypto"):
            raise ValueError(
                f"lane must be 'equity' or 'crypto', got {v!r}"
            )
        return v2


@router.get("/admin/patterns/universe")
async def universe_list(
    include_inactive: bool = Query(False),
    _user: dict = Depends(get_current_user),
):
    """List watchlist symbols. Defaults to active only."""
    q: dict = {} if include_inactive else {"active": {"$ne": False}}
    rows = await db[PATTERNS_UNIVERSE].find(q, {"_id": 0}).sort(
        "symbol", 1,
    ).to_list(1000)
    return {
        "items": rows, "count": len(rows),
        "doctrine": (
            "Watchlist scope only — adding a symbol grants no execution "
            "authority. Brains may opt in to scanning it; seat holder "
            "still decides every action."
        ),
    }


@router.post("/admin/patterns/universe")
async def universe_add(
    body: UniverseSymbolIn,
    user: dict = Depends(get_current_user),
):
    """Add (or reactivate) a symbol in the watchlist. Idempotent."""
    doc = {
        "symbol": body.symbol,
        "note": body.note,
        "active": body.active,
        "lane": body.lane,
        "added_by": user.get("email") or "operator",
        "updated_at": _now_iso(),
    }
    existing = await db[PATTERNS_UNIVERSE].find_one(
        {"symbol": body.symbol}, {"_id": 0},
    )
    if existing:
        await db[PATTERNS_UNIVERSE].update_one(
            {"symbol": body.symbol}, {"$set": doc},
        )
        return {"ok": True, "action": "updated", "symbol": body.symbol}
    doc["added_at"] = _now_iso()
    await db[PATTERNS_UNIVERSE].insert_one(doc)
    return {"ok": True, "action": "inserted", "symbol": body.symbol}


@router.delete("/admin/patterns/universe/{symbol}")
async def universe_remove(
    symbol: str,
    hard: bool = Query(False, description="if true, delete; else soft-deactivate"),
    user: dict = Depends(get_current_user),
):
    """Remove a symbol. Default = soft delete (active=false). `hard=true`
    removes the row entirely."""
    symbol = symbol.upper().strip()
    if hard:
        result = await db[PATTERNS_UNIVERSE].delete_one({"symbol": symbol})
        if result.deleted_count == 0:
            raise HTTPException(404, f"symbol {symbol!r} not in universe")
        return {"ok": True, "action": "hard_deleted", "symbol": symbol}
    result = await db[PATTERNS_UNIVERSE].update_one(
        {"symbol": symbol},
        {"$set": {
            "active": False,
            "updated_at": _now_iso(),
            "deactivated_by": user.get("email") or "operator",
        }},
    )
    if result.matched_count == 0:
        raise HTTPException(404, f"symbol {symbol!r} not in universe")
    return {"ok": True, "action": "soft_deactivated", "symbol": symbol}


# ──────────────────────── symbol metadata read ────────────────────────

@router.get("/admin/symbol-metadata")
async def symbol_metadata_list(
    symbol: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=1000),
    _user: dict = Depends(get_current_user),
):
    """Cached per-symbol facts (float, market cap, sector, CIK)."""
    q: dict = {"symbol": symbol.upper()} if symbol else {}
    rows = await db[SYMBOL_METADATA].find(q, {"_id": 0}).sort(
        "refreshed_at", -1,
    ).to_list(limit)
    return {"items": rows, "count": len(rows)}


# ──────────────────────── alt-data reads ────────────────────────

@router.get("/admin/alt-data/filings")
async def filings_list(
    symbol: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=500),
    _user: dict = Depends(get_current_user),
):
    """Recent Form-4 filings index. Descriptive evidence only."""
    q: dict = {}
    if symbol:
        q["symbol"] = symbol.upper()
    rows = await db[ALT_DATA_FILINGS].find(q, {"_id": 0}).sort(
        "filing_date", -1,
    ).to_list(limit)
    return {"items": rows, "count": len(rows)}


@router.get("/admin/alt-data/macro")
async def macro_list(
    series_id: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=2000),
    _user: dict = Depends(get_current_user),
):
    """Cached FRED macro series observations. Descriptive evidence only."""
    q: dict = {}
    if series_id:
        q["series_id"] = series_id.upper()
    rows = await db[ALT_DATA_MACRO].find(q, {"_id": 0}).sort(
        "date", -1,
    ).to_list(limit)
    return {"items": rows, "count": len(rows)}
