"""Seat reader — hot-path lookups against the in-memory cache.

Doctrine pin (2026-07-01, operator directive):
    "Signal/Risk runs in memory. Broker submit. Local receipt first.
     Small transactional DB second. Mongo third."

The trader does NOT hit Mongo when picking who's in each seat. The
in-memory `state` module is refreshed from Mongo in the background
every `TRADER_CACHE_REFRESH_SEC` (default 60s). If Mongo is down
the cache serves the last-known-good values — from SQLite on cold
boot, and from DEFAULT_SEATS on virgin deploy.

The functions here are kept `async def` for API compatibility with
the previous Mongo-backed signature, but they NEVER await anything.
The `db` parameter is accepted and ignored so `main.py` can call
these without an if-branch.
"""
from __future__ import annotations

from typing import Optional

from trader import state


# Re-export so callers importing seat.DEFAULT_SEATS keep working.
DEFAULT_SEATS = state.DEFAULT_SEATS


async def get_lane_seats(db, lane: str) -> dict[str, Optional[str]]:
    """Return all 4 role-holders for the lane. In-memory. Never blocks."""
    return state.get_lane_seats(lane)


async def governor_multiplier(db, lane: str) -> float:
    """Governor's risk multiplier for the lane (bounded [0.0, 2.0])."""
    return state.governor_multiplier(lane)
