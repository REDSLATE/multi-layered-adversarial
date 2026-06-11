"""Admin route for the shadow-outcome engine.

Operator endpoint to backfill `outcome_join` envelopes from end-of-day
prices when there are no real broker fills to walk back. Pairs with
`shared/doctrine/shadow_outcome.py`.

Endpoints:
  * POST /api/admin/outcome-join/shadow-close
      - dry_run (bool, default false): preview the joins without
        writing to the database. Returns the same sample envelope as
        a real run, with `joined` counting hypothetical attachments.
      - max_rows (int, default 250): cap on intents considered per
        run (keeps the StockFit budget bounded).
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from auth import get_current_user
from shared.doctrine.shadow_outcome import run_shadow_close

router = APIRouter(
    prefix="/admin/outcome-join", tags=["outcome-join"],
)


class ShadowCloseIn(BaseModel):
    dry_run: bool = Field(default=False)
    max_rows: int = Field(default=250, ge=1, le=2000)


@router.post("/shadow-close")
async def shadow_close_endpoint(
    body: Optional[ShadowCloseIn] = None,
    _user: dict = Depends(get_current_user),
) -> dict:
    payload = body or ShadowCloseIn()
    return await run_shadow_close(
        dry_run=payload.dry_run, max_rows=payload.max_rows,
    )
