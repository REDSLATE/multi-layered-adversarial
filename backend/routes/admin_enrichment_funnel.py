"""Enrichment Funnel — silent-data-gap visibility for the operator.

Doctrine pin (2026-02-19, operator directive after live UI evidence):
    The remaining production risk on this stack is not UI drift — the
    NO_DATA doctrine short-circuit renders the "no evidence" state
    honestly now. The remaining risk is the **silent data gap
    upstream** of that: an active watchlist symbol on which no brain
    ever emits an intent, so nothing gets classified, nothing gets
    enriched, nothing hits the doctrine, and the symbol goes dark
    without an operator-visible signal.

    This endpoint compresses the whole funnel into one queryable row
    per active-watchlist symbol:

        active watchlist symbol
          → recent intent?
          → enrichment attempted?
          → enrichment result (live / failed / no_symbol)?
          → provenance stamp on the persisted snapshot?

    Operator can spot in one glance which symbols went DARK (never
    emitted), which had LOUD failures (enricher raised), which had
    SILENT gaps (intents but no `enrichment_status` stamp — brain
    shipped raw doctrine_snapshot with no upstream enrichment).

Route: `GET /api/admin/enrichment-funnel?lane=equity&minutes=60`
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query

from auth import get_current_user
from db import db


# `api_router` in server.py has `prefix="/api"`, so THIS router
# registers WITHOUT the `/api` prefix — final path becomes
# `/api/admin/enrichment-funnel`.
router = APIRouter(tags=["admin", "telemetry"])


# Per-row `enrichment_status` synthesized bucket:
#   * "dark"      — zero intents on this symbol in the window
#   * "live"      — at least one intent with snapshot.enrichment_status="live"
#   * "failed"    — intents present, ALL stamps were "failed"
#   * "no_symbol" — intents present, ALL stamps were "no_symbol"
#   * "missing"   — intents present, NO stamp at all (raw ship-through)
#   * "mixed"     — intents present with a mix of stamped + missing states
#
# `provenance`: aggregate over the window
#   * "none"        — no intents (dark)
#   * "brain_full"  — at least one intent stamped "live" (brain-side enricher ran)
#   * "brain_partial" — intents present but stamps are "failed"/"no_symbol"
#   * "unstamped"   — intents present with NO enrichment_status field
_STATUS_BUCKETS = ("live", "failed", "no_symbol", "missing")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _row_status(counts: Dict[str, int]) -> str:
    live = counts.get("live", 0)
    failed = counts.get("failed", 0)
    no_symbol = counts.get("no_symbol", 0)
    missing = counts.get("missing", 0)
    total = live + failed + no_symbol + missing
    if total == 0:
        return "dark"
    populated = live + failed + no_symbol
    if live > 0 and (failed + no_symbol + missing) > 0:
        return "mixed"
    if live > 0:
        return "live"
    if failed > 0 and no_symbol == 0 and missing == 0:
        return "failed"
    if no_symbol > 0 and failed == 0 and missing == 0:
        return "no_symbol"
    if missing > 0 and populated == 0:
        return "missing"
    return "mixed"


def _row_provenance(counts: Dict[str, int]) -> str:
    live = counts.get("live", 0)
    failed = counts.get("failed", 0)
    no_symbol = counts.get("no_symbol", 0)
    missing = counts.get("missing", 0)
    total = live + failed + no_symbol + missing
    if total == 0:
        return "none"
    if live > 0:
        return "brain_full"
    if missing > 0 and (failed + no_symbol) == 0:
        return "unstamped"
    return "brain_partial"


@router.get("/admin/enrichment-funnel")
async def enrichment_funnel(
    lane: str = Query("equity", pattern="^(equity|crypto)$"),
    minutes: int = Query(60, ge=1, le=1440),
    _user=Depends(get_current_user),
) -> Dict[str, Any]:
    """Per-active-watchlist-symbol enrichment-funnel snapshot.

    Response:
        {
          "lane": "equity",
          "minutes": 60,
          "active_universe": 20,
          "recent_intent_symbols": 12,
          "dark_symbols": 8,
          "enrichment_attempted": 45,
          "enrichment_success": 30,
          "enrichment_failed": 15,
          "symbols": [
            {
              "symbol": "NVDA",
              "recent_intents": 3,
              "last_intent_ts": "2026-02-19T15:22:11+00:00",
              "enrichment_status": "live",
              "provenance": "brain_full"
            },
            ...
          ]
        }

    Rows are sorted:
        1. `dark` symbols first (biggest gap = top of dashboard)
        2. then `missing` (unstamped raw ships)
        3. then `failed` (loud broker/upstream errors)
        4. then `mixed` and `live` (working symbols at the bottom)
    """
    now = _now()
    window_start = now - timedelta(minutes=minutes)

    # ── active watchlist ──
    watchlist_docs = await db.patterns_universe.find(
        {"active": True, "lane": lane},
        {"symbol": 1, "_id": 0},
    ).to_list(length=1000)
    watchlist = sorted({d["symbol"] for d in watchlist_docs if d.get("symbol")})
    active_universe = len(watchlist)

    # ── intents in window (only watchlist symbols) ──
    # `(symbol, ingest_ts)` compound index landed earlier this session
    # covers this filter. Projection kept minimal so we don't drag
    # 4KB doctrine_snapshots for millions of rows.
    per_symbol_counts: Dict[str, Dict[str, int]] = {
        s: {k: 0 for k in _STATUS_BUCKETS} for s in watchlist
    }
    per_symbol_last_ts: Dict[str, Optional[datetime]] = {s: None for s in watchlist}

    cursor = db.shared_intents.find(
        {
            "ingest_ts": {"$gte": window_start},
            "lane": lane,
            "symbol": {"$in": watchlist},
        },
        {
            "symbol": 1,
            "ingest_ts": 1,
            "snapshot.enrichment_status": 1,
            "_id": 0,
        },
    )
    async for doc in cursor:
        sym = doc.get("symbol")
        if sym not in per_symbol_counts:
            continue
        snap = doc.get("snapshot") or {}
        status = snap.get("enrichment_status")
        bucket = status if status in ("live", "failed", "no_symbol") else "missing"
        per_symbol_counts[sym][bucket] += 1
        ts = doc.get("ingest_ts")
        if ts is not None:
            prev = per_symbol_last_ts.get(sym)
            if prev is None or ts > prev:
                per_symbol_last_ts[sym] = ts

    # ── per-symbol rows + totals ──
    rows: List[Dict[str, Any]] = []
    recent_intent_symbols = 0
    enrichment_attempted = 0
    enrichment_success = 0
    enrichment_failed = 0
    for sym in watchlist:
        counts = per_symbol_counts[sym]
        recent = sum(counts.values())
        if recent > 0:
            recent_intent_symbols += 1
        enrichment_attempted += counts["live"] + counts["failed"] + counts["no_symbol"]
        enrichment_success += counts["live"]
        enrichment_failed += counts["failed"] + counts["no_symbol"]
        last_ts = per_symbol_last_ts[sym]
        rows.append({
            "symbol": sym,
            "recent_intents": recent,
            "last_intent_ts": last_ts.isoformat() if last_ts else None,
            "enrichment_status": _row_status(counts),
            "provenance": _row_provenance(counts),
        })

    # Sort: dark first, then missing/failed, then mixed/live.
    _sort_priority = {
        "dark": 0, "missing": 1, "failed": 2, "no_symbol": 3,
        "mixed": 4, "live": 5,
    }
    rows.sort(key=lambda r: (_sort_priority.get(r["enrichment_status"], 9), r["symbol"]))

    dark_symbols = sum(1 for r in rows if r["enrichment_status"] == "dark")

    return {
        "lane": lane,
        "minutes": minutes,
        "active_universe": active_universe,
        "recent_intent_symbols": recent_intent_symbols,
        "dark_symbols": dark_symbols,
        "enrichment_attempted": enrichment_attempted,
        "enrichment_success": enrichment_success,
        "enrichment_failed": enrichment_failed,
        "symbols": rows,
    }
