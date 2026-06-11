"""Live parabolic-phase counts across the equity universe."""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List

from fastapi import APIRouter, Depends

from auth import get_current_user
from db import db

logger = logging.getLogger("risedual.parabolic_phase_admin")
router = APIRouter(prefix="/admin/parabolic", tags=["parabolic-phase"])


@router.get("/phases")
async def get_phase_counts(_user: dict = Depends(get_current_user)) -> Dict[str, Any]:
    """Return live counts per parabolic phase across the equity universe.

    Pulled from the most recent doctrine sidecar per symbol. Operator
    uses this to see in real-time which tickers the brains classify as
    accumulating vs parabolic vs topping vs fading — the same view the
    brains have, exposed for situational awareness.
    """
    # Pull the most recent sidecar per symbol from the last ~5 min
    pipeline: List[Dict[str, Any]] = [
        {"$match": {"lane": "equity"}},
        {"$sort": {"ts": -1}},
        {"$group": {
            "_id": "$symbol",
            "snapshot": {"$first": "$snapshot"},
            "ts": {"$first": "$ts"},
        }},
        {"$limit": 200},
    ]
    rows = await db["doctrine_sidecars"].aggregate(pipeline).to_list(200)

    counts = {
        "accumulation": 0,
        "parabolic": 0,
        "topping": 0,
        "fade": 0,
        "neutral": 0,
        "unknown": 0,
    }
    symbols: Dict[str, List[Dict[str, Any]]] = {k: [] for k in counts}
    for r in rows:
        snap = r.get("snapshot") or {}
        phase = str(snap.get("parabolic_phase", "unknown")).lower()
        if phase not in counts:
            phase = "unknown"
        counts[phase] += 1
        symbols[phase].append({
            "symbol": r.get("_id"),
            "velocity_5m": snap.get("velocity_5m"),
            "vwap_distance_pct": snap.get("vwap_distance_pct"),
            "rvol_acceleration": snap.get("rvol_acceleration"),
            "peak_drop_pct": snap.get("peak_drop_pct"),
        })
    return {
        "counts": counts,
        "symbols": symbols,
        "total_classified": sum(counts.values()),
    }
