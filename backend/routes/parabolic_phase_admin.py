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


# 8s matches the mc_shelly dashboard timeout — Atlas reads that take
# longer than this are treated as unavailable so the strip degrades
# gracefully instead of leaking a raw `NetworkTimeout` exception into
# the operator's screen.
_MONGO_READ_TIMEOUT_S = 8.0


def _empty_counts() -> Dict[str, int]:
    return {
        "accumulation": 0, "parabolic": 0, "topping": 0,
        "fade": 0, "neutral": 0, "unknown": 0,
    }


@router.get("/phases")
async def get_phase_counts(_user: dict = Depends(get_current_user)) -> Dict[str, Any]:
    """Return live counts per parabolic phase across the equity universe.

    Pulled from the most recent doctrine sidecar per symbol. Operator
    uses this to see in real-time which tickers the brains classify as
    accumulating vs parabolic vs topping vs fading — the same view the
    brains have, exposed for situational awareness.

    Degrades gracefully on Atlas failure (timeout or read error): the
    endpoint always returns HTTP 200 with zero counts + an `error` field
    the strip can render softly. Doctrine: the dashboard never blocks
    on Atlas — same pin as `mc_shelly` (2026-07-02).
    """
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
    counts = _empty_counts()
    symbols: Dict[str, List[Dict[str, Any]]] = {k: [] for k in counts}

    try:
        rows = await asyncio.wait_for(
            db["doctrine_sidecars"].aggregate(pipeline).to_list(200),
            timeout=_MONGO_READ_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "parabolic_phase.get_phase_counts: Atlas read timed out "
            "after %.1fs — returning empty counts.",
            _MONGO_READ_TIMEOUT_S,
        )
        return {
            "counts": counts, "symbols": symbols, "total_classified": 0,
            "error": "mongo_timeout",
            "message": (
                f"MongoDB Atlas read timed out after {_MONGO_READ_TIMEOUT_S:.0f}s. "
                f"Phase map paused; retry when Atlas recovers."
            ),
        }
    except Exception as exc:  # noqa: BLE001
        # Same defensive net as mc_shelly — any Atlas surprise
        # (connection reset, auth blip, driver bug) gets caught and
        # summarized rather than crashing the tile.
        logger.warning(
            "parabolic_phase.get_phase_counts: %s: %s",
            type(exc).__name__, str(exc)[:200],
        )
        return {
            "counts": counts, "symbols": symbols, "total_classified": 0,
            "error": "mongo_error",
            "message": f"{type(exc).__name__}: {exc}"[:240],
        }

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
        "counts": counts, "symbols": symbols,
        "total_classified": sum(counts.values()),
    }
