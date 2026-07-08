"""Per-Lane Capital Cap Ledger — read-only admin endpoints.

Ops-facing surface for the atomic reservation store implemented in
`shared/capital/ledger.py`. Tier-2 roles (Auditor, Governor,
Strategist) and the frontend headroom tile read via
`get_lane_headroom` — no writes, no race exposure.

Doctrine:
    * Every endpoint is READ-ONLY. Writes to the ledger happen
      only through the executor path (`reserve_capital`) and the
      broker-reconcile / position-close path (`release_capital`),
      NEVER via HTTP.
    * Admin-authenticated. The endpoints surface reservation
      details (intent_ids, amounts, ages) that are operator-only.
"""
from __future__ import annotations

from typing import Any, Dict, Literal

from fastapi import APIRouter, Depends, HTTPException, Path

from auth import get_current_user
from shared.capital.ledger import (
    VALID_LANES,
    get_all_headroom,
    get_lane_headroom,
    get_open_reservations,
)


router = APIRouter(prefix="/admin/capital", tags=["admin", "capital"])


@router.get("/headroom")
async def get_headroom(
    _user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """Composite headroom for all lanes.

    Response shape:
        {
          "equity": {total, reserved, available, utilization_pct, ...},
          "crypto": {total, reserved, available, utilization_pct, ...},
        }

    A `None` value under a lane key means `init_ledger` has not
    been called yet for that lane (fresh boot before lifespan
    init completes, or misconfigured env caps).
    """
    return await get_all_headroom()


@router.get("/headroom/{lane}")
async def get_lane_headroom_endpoint(
    lane: Literal["equity", "crypto"] = Path(...),
    _user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """Per-lane headroom detail."""
    if lane not in VALID_LANES:
        raise HTTPException(
            status_code=400,
            detail=f"invalid lane {lane!r} (must be one of {VALID_LANES})",
        )
    result = await get_lane_headroom(lane)
    if result is None:
        raise HTTPException(
            status_code=404,
            detail=f"ledger doc for lane={lane!r} not initialised — "
                   f"check init_ledger wired at lifespan startup",
        )
    return result


@router.get("/reservations/{lane}")
async def get_reservations_endpoint(
    lane: Literal["equity", "crypto"] = Path(...),
    limit: int = 50,
    _user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """Currently-open reservations for the lane (newest first)."""
    if lane not in VALID_LANES:
        raise HTTPException(
            status_code=400,
            detail=f"invalid lane {lane!r} (must be one of {VALID_LANES})",
        )
    limit = max(1, min(limit, 200))  # clamp
    reservations = await get_open_reservations(lane, limit=limit)
    return {
        "lane": lane,
        "count": len(reservations),
        "limit": limit,
        "reservations": reservations,
    }
