"""Outcome-join chain diagnostics + manual backfill.

The live position close path (`shared/live_positions.py`) already calls
`join_outcome_to_doctrine` on every close. This admin layer adds:

  * `/api/admin/outcome-join/health` — visibility into the chain:
    how many sidecars, how many joined, how many closed positions exist,
    and how many of those closes successfully attached an outcome.
  * `/api/admin/outcome-join/backfill` — walks closed positions whose
    doctrine_sidecars row is missing an `outcome_join` envelope and
    fires the join from server-side. Idempotent (re-running is safe;
    the join helper short-circuits when an envelope already exists).

Read-only by default — backfill must be invoked explicitly with a POST.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from auth import get_current_user
from db import db
from namespaces import DOCTRINE_SIDECARS, SHARED_INTENTS, SHARED_LIVE_POSITIONS
from shared.doctrine.outcome_join import join_outcome_to_doctrine

logger = logging.getLogger("risedual.outcome_join_admin")

router = APIRouter(prefix="/admin/outcome-join", tags=["outcome-join"])


@router.get("/health")
async def health(_user: dict = Depends(get_current_user)) -> Dict[str, Any]:  # noqa: B008
    """Diagnostic snapshot of the outcome-join chain."""
    sidecars_total = await db[DOCTRINE_SIDECARS].count_documents({})
    sidecars_joined = await db[DOCTRINE_SIDECARS].count_documents(
        {"outcome_join": {"$exists": True}}
    )
    intents_total = await db[SHARED_INTENTS].count_documents({})
    positions_total = await db[SHARED_LIVE_POSITIONS].count_documents({})
    positions_closed = await db[SHARED_LIVE_POSITIONS].count_documents({"state": "closed"})
    positions_closed_with_intent = await db[SHARED_LIVE_POSITIONS].count_documents(
        {"state": "closed", "intent_id": {"$exists": True, "$ne": None}}
    )

    # Walk the closed positions to count actual orphans (closed w/ intent
    # but no doctrine_join). Bounded sample — operator just needs the
    # order-of-magnitude signal.
    sample_limit = 1000
    closed_with_intent = await db[SHARED_LIVE_POSITIONS].find(
        {"state": "closed", "intent_id": {"$exists": True, "$ne": None}},
        {"_id": 0, "intent_id": 1, "position_id": 1, "lane": 1, "symbol": 1, "closed_at": 1, "stack": 1},
    ).sort("closed_at", -1).limit(sample_limit).to_list(sample_limit)

    intent_ids = [p["intent_id"] for p in closed_with_intent if p.get("intent_id")]
    joined_intent_ids: set[str] = set()
    if intent_ids:
        cursor = db[DOCTRINE_SIDECARS].find(
            {"intent_id": {"$in": intent_ids}, "outcome_join": {"$exists": True}},
            {"_id": 0, "intent_id": 1},
        )
        async for r in cursor:
            joined_intent_ids.add(r["intent_id"])

    orphans = [p for p in closed_with_intent if p["intent_id"] not in joined_intent_ids]
    join_rate = (
        round(len(joined_intent_ids) / len(intent_ids), 4) if intent_ids else None
    )

    return {
        "totals": {
            "doctrine_sidecars": sidecars_total,
            "doctrine_sidecars_joined": sidecars_joined,
            "join_ratio_sidecars": round(sidecars_joined / sidecars_total, 4)
            if sidecars_total
            else 0.0,
            "shared_intents": intents_total,
            "positions": positions_total,
            "positions_closed": positions_closed,
            "positions_closed_with_intent_id": positions_closed_with_intent,
        },
        "closed_position_sample": {
            "sample_limit": sample_limit,
            "sample_size": len(closed_with_intent),
            "joined_in_sample": len(joined_intent_ids),
            "orphans_in_sample": len(orphans),
            "join_rate_in_sample": join_rate,
            "first_orphan_examples": orphans[:5],
        },
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "doctrine_note": (
            "An orphan is a closed position whose doctrine_sidecars row exists "
            "but has no `outcome_join` envelope. Backfill with POST "
            "/api/admin/outcome-join/backfill to retroactively attach outcomes."
        ),
    }


class BackfillRequest(BaseModel):
    older_than_hours: Optional[float] = None  # only backfill closes older than this
    lane: Optional[str] = None
    dry_run: bool = True
    limit: int = 500


@router.post("/backfill")
async def backfill(
    req: BackfillRequest,
    _user: dict = Depends(get_current_user),  # noqa: B008
) -> Dict[str, Any]:
    """Retroactively attach outcome envelopes onto historical
    doctrine_sidecars rows that were missed by the live join path.

    `dry_run=True` (default) returns the count + sample without writing.
    """
    q: Dict[str, Any] = {"state": "closed", "intent_id": {"$exists": True, "$ne": None}}
    if req.lane:
        q["lane"] = req.lane
    if req.older_than_hours:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=req.older_than_hours)
        q["closed_at"] = {"$lte": cutoff.isoformat()}

    candidates = await db[SHARED_LIVE_POSITIONS].find(q, {"_id": 0}).sort(
        "closed_at", -1
    ).limit(max(1, min(req.limit, 5000))).to_list(req.limit)

    inspected = len(candidates)
    would_join: List[Dict[str, Any]] = []
    joined_count = 0
    skipped_already_joined = 0
    skipped_no_sidecar = 0

    for pos in candidates:
        intent_id = pos.get("intent_id")
        sidecar = await db[DOCTRINE_SIDECARS].find_one(
            {"intent_id": intent_id},
            {"_id": 0, "outcome_join": 1},
        )
        if sidecar is None:
            skipped_no_sidecar += 1
            continue
        if sidecar.get("outcome_join"):
            skipped_already_joined += 1
            continue

        # Reconstruct the outcome from the position's final fill.
        final_fill = (pos.get("fills") or [{}])[-1]
        outcome_label = (
            pos.get("outcome_label")
            or final_fill.get("outcome_label")
            or "scratch"
        )
        pnl_usd = pos.get("pnl_usd") if pos.get("pnl_usd") is not None else final_fill.get("pnl_usd")
        pnl_pct = pos.get("pnl_pct") if pos.get("pnl_pct") is not None else final_fill.get("pnl_pct")

        if req.dry_run:
            would_join.append({
                "intent_id": intent_id,
                "position_id": pos.get("position_id"),
                "lane": pos.get("lane"),
                "symbol": pos.get("symbol"),
                "outcome_label": outcome_label,
                "pnl_usd": pnl_usd,
                "closed_at": pos.get("closed_at"),
                "stack": pos.get("stack"),
            })
        else:
            ok = await join_outcome_to_doctrine(
                intent_id=intent_id,
                position_id=pos.get("position_id"),
                lane=pos.get("lane"),
                symbol=pos.get("symbol"),
                outcome_label=outcome_label,
                pnl_usd=pnl_usd,
                pnl_pct=pnl_pct,
                opened_at=pos.get("opened_at"),
                closed_at=pos.get("closed_at"),
                closing_actor=pos.get("closing_actor") or "backfill",
                extra={
                    "stack": pos.get("stack"),
                    "direction": pos.get("direction"),
                    "backfill": True,
                },
            )
            if ok:
                joined_count += 1
            else:
                # Treat as already joined (race) — the helper is fail-soft.
                skipped_already_joined += 1

    return {
        "dry_run": req.dry_run,
        "filters": {
            "lane": req.lane,
            "older_than_hours": req.older_than_hours,
            "limit": req.limit,
        },
        "inspected": inspected,
        "would_join_count" if req.dry_run else "joined": (
            len(would_join) if req.dry_run else joined_count
        ),
        "skipped_already_joined": skipped_already_joined,
        "skipped_no_sidecar": skipped_no_sidecar,
        "would_join_sample": would_join[:10] if req.dry_run else [],
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
