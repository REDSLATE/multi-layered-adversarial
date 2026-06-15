"""Admin routes for QuiverQuant alt-data sync + status.

Mirrors the shape of other alt_data admin routes. All endpoints are
JWT-protected via `get_current_user`.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from auth import get_current_user
from db import db
from shared.alt_data import quiver_quant as qq

router = APIRouter(prefix="/admin/alt-data/quiver", tags=["alt-data", "quiver"])


@router.get("/status")
async def quiver_status(_user: dict = Depends(get_current_user)) -> dict:
    """Configuration + last-known counts. Safe with no API key."""
    insider_n  = await db[qq.COLL_INSIDER].estimated_document_count()
    congress_n = await db[qq.COLL_CONGRESS].estimated_document_count()
    patents_n  = await db[qq.COLL_PATENTS].estimated_document_count()
    return {
        "configured": qq.is_configured(),
        "base_url": qq.QUIVER_BASE_URL,
        "collections": {
            "insider":  {"name": qq.COLL_INSIDER,  "count": insider_n},
            "congress": {"name": qq.COLL_CONGRESS, "count": congress_n},
            "patents":  {"name": qq.COLL_PATENTS,  "count": patents_n},
        },
        "ts": datetime.now(timezone.utc).isoformat(),
    }


class SyncBody(BaseModel):
    patent_tickers: list[str] = Field(default_factory=list)


@router.post("/sync")
async def quiver_sync(
    body: SyncBody = SyncBody(),
    _user: dict = Depends(get_current_user),
) -> dict:
    """Pull latest insider + congress feeds; optionally patent momentum
    for a list of tickers (Tier 1 only — graceful 403 if not entitled)."""
    return await qq.sync_all(db, patent_tickers=body.patent_tickers)


@router.get("/insider/latest")
async def get_latest_insider(
    _user: dict = Depends(get_current_user),
    ticker: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
) -> list[dict]:
    q = {"ticker": ticker.upper()} if ticker else {}
    rows = await db[qq.COLL_INSIDER].find(q, {"_id": 0, "raw": 0}) \
        .sort("transaction_date", -1).to_list(length=limit)
    return rows


@router.get("/congress/latest")
async def get_latest_congress(
    _user: dict = Depends(get_current_user),
    ticker: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
) -> list[dict]:
    q = {"ticker": ticker.upper()} if ticker else {}
    rows = await db[qq.COLL_CONGRESS].find(q, {"_id": 0, "raw": 0}) \
        .sort("transaction_date", -1).to_list(length=limit)
    return rows


@router.get("/patents/{ticker}")
async def get_patents_for_ticker(
    ticker: str,
    _user: dict = Depends(get_current_user),
) -> list[dict]:
    rows = await db[qq.COLL_PATENTS].find(
        {"ticker": ticker.upper()}, {"_id": 0, "raw": 0},
    ).sort("as_of_date", -1).to_list(length=200)
    return rows
