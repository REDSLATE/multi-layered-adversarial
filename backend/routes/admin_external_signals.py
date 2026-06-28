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


# ──────────────────────── Seat-bound cleaned context ────────────────────────


@router.get("/admin/external-signals/seat-context")
async def seat_context(
    _user=Depends(get_current_user),
    symbol: Optional[str] = Query(default=None, description="filter by ticker"),
    hours: int = Query(default=24, ge=1, le=72),
    limit: int = Query(default=50, ge=1, le=200),
):
    """Return CLEANED witness context for the Seat-bound view.

    Doctrine pin (TRIAL COURT):
        The witness page displays everything (raw). RoadGuard labels
        suspicious clusters (SPAM, DUPLICATE_BURST, FLIP_FLOP,
        SOFT_NEWS_CLUSTER, SOURCE_DRIFT). This endpoint returns
        ONLY rows that carry NO RoadGuard labels — i.e. witnesses
        that survived the noise filter.

        Even so, the rows returned are STILL `verifier_status=UNTRUSTED`
        and `influence_allowed=False`. The Seat sees them as
        ADVISORY context, not authority. This endpoint does not
        change that. The cleaned set is just "less noise" — not
        "promoted to trusted."

        The response also reports what was filtered out (counts per
        label) so the operator can audit the filter's behavior. If
        SOFT_NEWS_CLUSTER is silently dropping 60% of NVDA witnesses,
        the operator deserves to see that number explicitly, not
        discover it later.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    base_q: dict[str, Any] = {"received_at": {"$gte": cutoff}}
    if symbol:
        base_q["symbol"] = symbol.strip().upper()

    # Cleaned: no roadguard_labels OR explicitly empty
    cleaned_q = {
        **base_q,
        "$or": [
            {"roadguard_labels": {"$exists": False}},
            {"roadguard_labels": {"$size": 0}},
        ],
    }
    cleaned = await db[EXTERNAL_SIGNALS].find(
        cleaned_q, _PROJECTION_RECENT,
    ).sort("received_at", -1).to_list(limit)

    # Filter audit: what got filtered out, by label
    filtered_q = {
        **base_q,
        "roadguard_labels": {"$exists": True, "$not": {"$size": 0}},
    }
    total_filtered = await db[EXTERNAL_SIGNALS].count_documents(filtered_q)
    pipeline = [
        {"$match": filtered_q},
        {"$unwind": "$roadguard_labels"},
        {"$group": {"_id": "$roadguard_labels", "n": {"$sum": 1}}},
        {"$sort": {"n": -1}},
    ]
    filtered_by_label: dict[str, int] = {}
    async for d in db[EXTERNAL_SIGNALS].aggregate(pipeline):
        filtered_by_label[d["_id"]] = d["n"]

    total_in_window = await db[EXTERNAL_SIGNALS].count_documents(base_q)

    return {
        "items": cleaned,
        "count": len(cleaned),
        "window_hours": hours,
        "totals": {
            "total_in_window": total_in_window,
            "cleaned_shown": len(cleaned),
            "filtered_out": total_filtered,
        },
        "filtered_by_label": filtered_by_label,
        "doctrine": (
            "SEAT-BOUND CLEANED CONTEXT. Rows here survived RoadGuard "
            "label filtering. They are still UNTRUSTED and "
            "influence_allowed=False. Read-only advisory context — "
            "the Seat does not act on these, the Seat is INFORMED by these. "
            "Verifier (future) decides if any source ever earns weight."
        ),
    }
