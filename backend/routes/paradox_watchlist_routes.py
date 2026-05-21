"""Paradox Coordinator v0 — watchlist admin surface.

Operator CRUD over `paradox_watchlist`. The scanner's PRIMARY
universe source. When empty, scan falls back to a hardcoded
top-liquid list (see `services/paradox_scanner.py`).

Endpoints (all JWT admin):
    GET    /admin/paradox/watchlist                    — list (active or all)
    POST   /admin/paradox/watchlist                    — add one or many
    DELETE /admin/paradox/watchlist/{symbol}           — remove
    POST   /admin/paradox/watchlist/{symbol}/toggle    — active/inactive
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel, Field

from auth import get_current_user
from db import db
from namespaces import PARADOX_WATCHLIST


router = APIRouter(prefix="/admin/paradox/watchlist", tags=["paradox-watchlist"])


VALID_LANES = ("equity", "crypto")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_symbol(s: str) -> str:
    s = (s or "").strip().upper()
    if not s:
        raise HTTPException(status_code=400, detail="symbol required")
    if len(s) > 16:
        raise HTTPException(status_code=400, detail="symbol too long")
    return s


def _serialize(doc: Dict[str, Any]) -> Dict[str, Any]:
    doc.pop("_id", None)
    for k in ("added_at",):
        v = doc.get(k)
        if isinstance(v, datetime):
            doc[k] = v.isoformat()
    return doc


# ─── schemas ──────────────────────────────────────────────────────────


class WatchlistEntry(BaseModel):
    symbol: str
    lane: str = "equity"
    note: Optional[str] = None

    def lane_validated(self) -> str:
        lane = (self.lane or "equity").lower()
        if lane not in VALID_LANES:
            raise HTTPException(
                status_code=400,
                detail=f"lane {lane!r} not in {list(VALID_LANES)}",
            )
        return lane


class AddRequest(BaseModel):
    entries: List[WatchlistEntry] = Field(min_length=1)


# ─── endpoints ────────────────────────────────────────────────────────


@router.get("")
async def list_watchlist(
    active_only: bool = Query(default=True),
    _user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    q: Dict[str, Any] = {}
    if active_only:
        q["active"] = True
    items: List[Dict[str, Any]] = []
    async for d in db[PARADOX_WATCHLIST].find(q).sort("added_at", -1):
        items.append(_serialize(d))
    return {"ok": True, "count": len(items), "items": items}


@router.post("")
async def add_watchlist(
    body: AddRequest,
    user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    added_by = user.get("email", "operator")
    out: List[Dict[str, Any]] = []
    for entry in body.entries:
        symbol = _normalize_symbol(entry.symbol)
        lane = entry.lane_validated()
        doc = {
            "symbol": symbol,
            "lane": lane,
            "note": (entry.note or "").strip() or None,
            "active": True,
            "added_by": added_by,
            "added_at": _now(),
        }
        await db[PARADOX_WATCHLIST].update_one(
            {"symbol": symbol},
            {"$set": doc},
            upsert=True,
        )
        out.append(_serialize(dict(doc)))
    return {"ok": True, "added": len(out), "items": out}


@router.delete("/{symbol}")
async def remove_watchlist(
    symbol: str = Path(...),
    _user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    symbol = _normalize_symbol(symbol)
    result = await db[PARADOX_WATCHLIST].delete_one({"symbol": symbol})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail=f"{symbol!r} not in watchlist")
    return {"ok": True, "removed": symbol}


@router.post("/{symbol}/toggle")
async def toggle_watchlist(
    symbol: str = Path(...),
    _user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    symbol = _normalize_symbol(symbol)
    doc = await db[PARADOX_WATCHLIST].find_one({"symbol": symbol})
    if not doc:
        raise HTTPException(status_code=404, detail=f"{symbol!r} not in watchlist")
    new_state = not bool(doc.get("active", True))
    await db[PARADOX_WATCHLIST].update_one(
        {"symbol": symbol},
        {"$set": {"active": new_state}},
    )
    return {"ok": True, "symbol": symbol, "active": new_state}
