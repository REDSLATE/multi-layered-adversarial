"""Witnesses Diagnostic — read-only "Untrusted Witnesses" panel.

Doctrine pin (2026-02-23, witness-council layer):
    External signals (Polygon news+sentiment, eventually Pine /
    Public / MTR) land in the `external_signals` holding cell as
    DEFAULT-HOSTILE: `verifier_status=UNTRUSTED`, `influence_allowed=False`.
    Nothing they write affects a trade. This endpoint is the
    operator's window into what witnesses are saying — read-only,
    no click-to-execute path, no mutation surface.

    Doctrine frame: TRIAL COURT, NOT A VOTING SYSTEM.
    Pine / Polygon / Public are witnesses, not authorities.
    Verifier (future) decides if any source earns weight.
    Until then, witness output is for operator situational
    awareness only.

Endpoints:
    GET  /api/admin/external-signals              recent witness rows
    GET  /api/admin/external-signals/credibility  per-source case files

Auth: operator JWT.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from auth import get_current_user
from db import db
from namespaces import EXTERNAL_SIGNALS, EXTERNAL_SOURCE_CREDIBILITY


router = APIRouter(tags=["admin"])


_PROJECTION_RECENT = {
    "_id": 0,
    "id": 1,
    "source": 1,
    "symbol": 1,
    "side": 1,
    "self_reported_confidence": 1,
    "event": 1,
    "reason": 1,
    "bar_close_ts": 1,
    "verifier_status": 1,
    "influence_allowed": 1,
    "received_at": 1,
}


@router.get("/admin/external-signals")
async def list_external_signals(
    _user=Depends(get_current_user),
    source: Optional[str] = Query(default=None, description="filter by witness source"),
    symbol: Optional[str] = Query(default=None, description="filter by ticker"),
    side: Optional[str] = Query(default=None, description="filter BUY/SELL/HOLD"),
    hours: int = Query(default=24, ge=1, le=720, description="lookback window"),
    limit: int = Query(default=100, ge=1, le=500),
):
    """Return recent witness rows for the Untrusted Witnesses panel.

    Read-only. No mutation, no execution side-effects. The panel
    renders these as dimmed cards with a status badge that
    explicitly says "UNTRUSTED — no execution influence" so the
    operator never confuses a witness alert for a brain signal.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    q: dict[str, Any] = {"received_at": {"$gte": cutoff}}
    if source:
        q["source"] = source.strip().lower()
    if symbol:
        q["symbol"] = symbol.strip().upper()
    if side:
        side_up = side.strip().upper()
        if side_up not in ("BUY", "SELL", "HOLD"):
            raise HTTPException(
                status_code=400,
                detail="side must be BUY, SELL, or HOLD",
            )
        q["side"] = side_up

    rows = await db[EXTERNAL_SIGNALS].find(
        q, _PROJECTION_RECENT,
    ).sort("received_at", -1).to_list(limit)

    # Totals snapshot for the panel header
    totals = {
        "total_24h": await db[EXTERNAL_SIGNALS].count_documents({
            "received_at": {
                "$gte": (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
            },
        }),
        "total_in_window": len(rows),
    }

    # Per-source breakdown so the operator sees which witness is loud
    pipeline = [
        {"$match": q},
        {"$group": {
            "_id": {"source": "$source", "side": "$side"},
            "n": {"$sum": 1},
        }},
        {"$sort": {"n": -1}},
    ]
    by_source: dict[str, dict[str, int]] = {}
    async for d in db[EXTERNAL_SIGNALS].aggregate(pipeline):
        src = d["_id"]["source"]
        sd = d["_id"]["side"]
        by_source.setdefault(src, {})[sd] = d["n"]

    return {
        "items": rows,
        "count": len(rows),
        "window_hours": hours,
        "totals": totals,
        "by_source": by_source,
        # Doctrine banner — surfaced in API too so any consumer is
        # reminded that these are advisory.
        "doctrine": (
            "TRIAL COURT, NOT A VOTING SYSTEM. "
            "External signals are default-hostile; influence_allowed=False "
            "until Verifier promotes the source. Nothing here moves a trade."
        ),
    }


@router.get("/admin/external-signals/credibility")
async def list_source_credibility(_user=Depends(get_current_user)):
    """Return the per-source credibility ledger snapshot.

    This is Verifier's case file for each witness source. Operator-
    readable; Verifier-writable. The panel renders one row per
    source with status, samples, win/loss counters, and the
    rolling verified_alpha.
    """
    rows = await db[EXTERNAL_SOURCE_CREDIBILITY].find(
        {}, {"_id": 0},
    ).sort("source", 1).to_list(None)
    return {
        "items": rows,
        "count": len(rows),
        "doctrine": (
            "Verifier-owned. Webhook may $setOnInsert default-hostile rows; "
            "promotion/demotion is Verifier's job. Phase progression: "
            "UNTRUSTED → WATCHLIST → TRUSTED (and reverse)."
        ),
    }
