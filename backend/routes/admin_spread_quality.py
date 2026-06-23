"""Spread-quality breakdown endpoint (2026-06-22).

Operator pin (verbatim):
    "Yes — add the /api/admin/spread-quality/breakdown endpoint.
    That turns this from a hidden data poison issue into an
    observable feed-health signal.
    Then the operator can immediately tell:
       Webull data bad everywhere
       versus:
       Webull data bad only on thin/unsupported symbols"

Doctrine: STRICTLY read-only. Aggregates `intent.snapshot.spread_quality`
across `shared_intents` over the requested window. Returns global
totals + per-symbol counts so the operator can spot whether bad
spreads are systemic (Webull-wide outage) or concentrated on thin
names (legitimate Webull no-coverage for micro-caps).

GET /api/admin/spread-quality/breakdown?hours=24&top=25

Response shape:

    {
      "hours": 24,
      "totals": {
        "live": 1840,
        "stale": 312,
        "sentinel": 97
      },
      "by_symbol": [
        {"symbol": "NVDA", "live": 220, "stale": 3, "sentinel": 0,
         "total": 223, "untrusted_pct": 1.3},
        {"symbol": "A",    "live": 0,   "stale": 14, "sentinel": 38,
         "total": 52,  "untrusted_pct": 100.0},
        ...
      ]
    }

`top` defaults to 25 — operator sees the worst 25 symbols by
combined stale+sentinel count. `untrusted_pct` lets the operator
sort the list by feed-health badness in one glance.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, Query

from auth import get_current_user
from db import db
from namespaces import SHARED_INTENTS

logger = logging.getLogger("risedual.spread_quality_breakdown")

router = APIRouter(
    prefix="/admin/spread-quality",
    tags=["admin-spread-quality"],
)


QUALITY_TAGS = ("live", "stale", "sentinel")


@router.get("/breakdown")
async def spread_quality_breakdown(
    hours: int = Query(default=24, ge=1, le=168),
    top: int = Query(default=25, ge=1, le=500),
    lane: str = Query(
        default="equity",
        pattern="^(equity|crypto|all)$",
        description="Filter by lane — operator's primary use case is "
                    "equity (where the 9999-bps sentinel poison originated)",
    ),
    _user: dict = Depends(get_current_user),  # noqa: B008
) -> Dict[str, Any]:
    """Aggregate `intent.snapshot.spread_quality` over the window so
    the operator can answer "is Webull broken everywhere, or only on
    thin symbols?" at a glance."""
    cutoff_iso = (
        datetime.now(timezone.utc) - timedelta(hours=hours)
    ).isoformat()

    q: Dict[str, Any] = {"ingest_ts": {"$gte": cutoff_iso}}
    if lane != "all":
        q["lane"] = lane

    cursor = db[SHARED_INTENTS].find(
        q,
        {
            "_id": 0,
            "symbol": 1,
            "snapshot": 1,
        },
    )

    totals: Dict[str, int] = {tag: 0 for tag in QUALITY_TAGS}
    per_symbol: Dict[str, Dict[str, int]] = defaultdict(
        lambda: {tag: 0 for tag in QUALITY_TAGS}
    )
    untagged = 0
    seen_intents = 0

    async for doc in cursor:
        seen_intents += 1
        snap = doc.get("snapshot") or {}
        # `spread_quality` is the canonical tag stamped by the
        # enricher (post-2026-06-22 patch). For pre-patch intents
        # the field is absent — count them under `untagged` so the
        # operator can see how much of the window predates the fix.
        q_raw = snap.get("spread_quality")
        if not isinstance(q_raw, str):
            untagged += 1
            continue
        q_clean = q_raw.strip().lower()
        if q_clean not in QUALITY_TAGS:
            # An unrecognized quality string is the same as
            # untagged from a feed-health POV.
            untagged += 1
            continue
        totals[q_clean] += 1
        sym = (doc.get("symbol") or "").upper() or "?"
        per_symbol[sym][q_clean] += 1

    # Rank symbols by combined untrusted count (stale + sentinel)
    # then by total — most concerning at the top so the operator
    # eye-balls the worst feed-quality offenders first.
    ranked: List[Dict[str, Any]] = []
    for sym, counts in per_symbol.items():
        tot = sum(counts.values())
        untrusted = counts["stale"] + counts["sentinel"]
        untrusted_pct = round((untrusted / tot) * 100.0, 1) if tot else 0.0
        ranked.append({
            "symbol": sym,
            "live": counts["live"],
            "stale": counts["stale"],
            "sentinel": counts["sentinel"],
            "total": tot,
            "untrusted_pct": untrusted_pct,
        })
    ranked.sort(
        key=lambda r: (r["sentinel"] + r["stale"], r["total"]),
        reverse=True,
    )

    return {
        "hours": hours,
        "lane": lane,
        "totals": totals,
        # Surface the legacy/pre-patch count so the operator can
        # decide whether a "low live count" is feed-health or simply
        # "the window predates the deploy."
        "untagged_pre_patch": untagged,
        "intents_observed": seen_intents,
        "by_symbol": ranked[:top],
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
