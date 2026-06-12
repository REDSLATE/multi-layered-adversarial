"""Shared position-close primitives — extracted to break a circular
import between `routes/runtime_position_close.py` and
`shared/intents.py` (2026-02-19).

Background: `post_intent` (in `shared/intents.py`) needs to dispatch
`action=CLOSE` intents to `close_position` (in `routes/runtime_
position_close.py`). And `close_position` needs to post the inverse-
side intent back through `post_intent`. That's a designed mutual
delegation — not a code smell — but the linter flagged it because
both modules transitively depended on each other through function-
level late imports.

This module owns the shared types + helpers that both sides need.
The runtime function references (`close_position`, `post_intent`)
remain as late imports inside the functions that call them, because
the recursion is a runtime concern, not a module-load concern.
"""
from __future__ import annotations

import os
from typing import Literal, Optional

from pydantic import BaseModel, Field

from namespaces import DISCUSSION_PARTICIPANTS


class CloseIn(BaseModel):
    """Request body for `POST /api/runtime/positions/close` and also
    the dispatch shape `post_intent` uses when it receives an
    `action=CLOSE` intent. Lives here so both can import statically."""
    symbol: str = Field(min_length=1, max_length=24)
    lane: Literal["equity", "crypto"]
    # Partial-close support: fraction ∈ (0, 1.0]. Default 1.0 = full
    # close. <=0 or >1 rejected at the boundary.
    fraction: float = Field(default=1.0, gt=0.0, le=1.0)
    rationale: str = Field(
        default="brain-initiated close",
        min_length=1, max_length=4000,
    )
    confidence: float = Field(default=0.9, ge=0.0, le=1.0)
    # Confidence on this close decision. Defaults high (0.9) because a
    # close intent is a different doctrinal beast — it's not "I think
    # this trade will work" but "I am exiting this position now". The
    # brain can override if it's expressing uncertainty about the exit.


def resolve_runtime_from_token(token: str) -> Optional[str]:
    """Map an X-Runtime-Token to the brain that holds it. Returns None
    when the token doesn't match any known brain. Shared between the
    route handler and the close-intent dispatcher."""
    for brain in DISCUSSION_PARTICIPANTS:
        expected = os.environ.get(f"{brain.upper()}_INGEST_TOKEN")
        if expected and token == expected:
            return brain
    return None


def inverse_side(broker_side: str) -> Literal["SELL", "COVER"]:
    """Map broker position side → the action that closes it.

    Alpaca returns side as 'long' / 'short' (lowercase). Webull and
    Kraken normalize to the same shape via the adapter layer.
    """
    s = (broker_side or "").lower()
    if s == "long":
        return "SELL"
    if s == "short":
        return "COVER"
    raise ValueError(f"unknown broker position side: {broker_side!r}")
