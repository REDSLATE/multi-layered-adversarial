"""Symbol Registry admin surface — read-only diagnostic view.

Doctrine (2026-07-15, iter-30 P3):
    The registry is source-of-truth for canonical ↔ broker symbol
    resolution. This admin endpoint exposes what the registry has
    learned so the operator can see WHY a symbol was excluded from
    a broker's execution universe (e.g. Webull returned
    `INVALID_SYMBOL` for HOTH) without grepping backend logs.

    Read-only by design. Deletes / manual overrides are intentionally
    NOT exposed — the correct way to "forgive" a symbol is to let
    the TTL expire (default 6h) and let the next adapter probe
    re-stamp the row.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, Query

from auth import get_current_user
from shared.broker.symbol_registry import registry_snapshot


logger = logging.getLogger("risedual.symbol_registry_admin")
router = APIRouter(
    prefix="/admin/symbol-registry", tags=["symbol-registry"],
)


@router.get("/")
async def list_registry(
    broker: Optional[str] = Query(None, description="Filter to one broker (e.g. 'webull')"),
    _user=Depends(get_current_user),
) -> dict:
    """Return every registry row (or every row for one broker).

    Payload shape:
        {
          "count": int,
          "rows": [
             {
               "canonical_symbol": "HOTH",
               "brokers": {
                 "webull": {
                   "instrument_id": null,
                   "tradable": false,
                   "reason": "no_quote",
                   "resolved_at": "...",
                   "expires_at": "..."
                 }
               },
               "updated_at": "..."
             },
             ...
          ]
        }
    """
    raw = await registry_snapshot(broker)
    rows = []
    for r in raw:
        rows.append({
            "canonical_symbol": r.get("_id"),
            "brokers": r.get("brokers") or {},
            "updated_at": r.get("updated_at"),
        })
    return {"ok": True, "count": len(rows), "rows": rows}
