"""Brain seat registry — runtime job assignment, mutable, NOT bound
to brain_id or doctrine.

Doctrine pin (operator directive, 2026-06-XX):

    brain_id  = who it is        (immutable — Camino is Camino)
    doctrine  = how it thinks    (immutable — bound to brain_id)
    seat      = what job it is    (mutable — assigned at runtime)

    Camino can be executor today, auditor tomorrow.
    But Camino still thinks like a trend-following brain.

Anti-pattern (the thing this module exists to prevent):

    if seat == "executor":
        doctrine = "momentum"     # ← NO. Never do this.

Correct pattern:

    doctrine = get_doctrine(brain_id)         # personality
    seat = get_current_seat(brain_id)         # today's job

Seats and what they imply (NOT what they DETERMINE):

    strategist  — generates trade hypotheses, posts to MC
    executor    — routes accepted intents to broker
    governor    — owns risk gates + portfolio-level brakes
    auditor     — reviews outputs, surfaces objections post-hoc

The seat is a *responsibility* tag the gate chain and the dashboard
read so they know what role to expect from this brain's output.
It does NOT change how the brain interprets the snapshot.

Implementation:
    * Default assignments hard-coded below — sensible starting point.
    * Mongo collection `brain_seat_assignments` overrides defaults.
    * Operator can rotate seats via the admin endpoint
      (POST /api/admin/brain-seats — to be wired separately).
    * Lookups are cached for 5 seconds so the brain runner doesn't
      hit Mongo on every tick.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional


logger = logging.getLogger("risedual.brain_seats")


SEAT_COLLECTION = "brain_seat_assignments"
SEATS = ("strategist", "executor", "governor", "auditor")


# Defaults — operator-tunable starting point. The intent is that
# all four brains are STRATEGISTS by default (they all generate
# hypotheses; the gate chain decides which proposals fire). Operator
# can promote one to executor / demote one to auditor at runtime
# without changing any brain's doctrine.
_DEFAULT_SEATS: dict[str, str] = {
    "camino": "strategist",
    "barracuda": "strategist",
    "hellcat": "strategist",
    "gto": "strategist",
}


# Cache: brain_id → (epoch_seconds, seat). 5-second TTL.
_CACHE: dict[str, tuple[float, str]] = {}
_CACHE_LOCK = asyncio.Lock()
_CACHE_TTL_SEC = 5.0


async def _read_override(brain_id: str) -> Optional[str]:
    """Read a Mongo-side seat override if one exists."""
    try:
        from db import db  # local import — db lazy-binds on env
        doc = await db[SEAT_COLLECTION].find_one(
            {"_id": brain_id}, {"seat": 1, "_id": 0},
        )
        if doc and isinstance(doc.get("seat"), str):
            return doc["seat"]
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "seat override lookup failed brain=%s err=%s — "
            "falling back to default", brain_id, exc,
        )
    return None


async def get_current_seat(brain_id: str) -> str:
    """Return the seat this brain is currently holding.

    Lookup order:
        1. 5-second per-brain cache
        2. Mongo override (`brain_seat_assignments` collection)
        3. Hard-coded default (all brains = strategist)

    Doctrine: this function MUST never raise. Brain runner uses it
    on every tick; an exception would brick the decision loop. On
    any failure it falls back to the default seat.
    """
    bid = (brain_id or "").lower().strip()
    if not bid:
        return "strategist"

    now = time.time()
    async with _CACHE_LOCK:
        cached = _CACHE.get(bid)
        if cached and (now - cached[0]) < _CACHE_TTL_SEC:
            return cached[1]

    override = await _read_override(bid)
    seat = override if override in SEATS else _DEFAULT_SEATS.get(bid, "strategist")

    async with _CACHE_LOCK:
        _CACHE[bid] = (time.time(), seat)
    return seat


async def set_seat(brain_id: str, seat: str) -> None:
    """Operator-triggered seat rotation. Writes an override doc and
    invalidates the cache for the affected brain.

    Raises ValueError if `seat` is not one of SEATS.
    """
    bid = (brain_id or "").lower().strip()
    s = (seat or "").lower().strip()
    if s not in SEATS:
        raise ValueError(f"unknown seat: {seat!r}; must be one of {SEATS}")
    from db import db
    await db[SEAT_COLLECTION].update_one(
        {"_id": bid},
        {"$set": {"seat": s, "updated_at": time.time()}},
        upsert=True,
    )
    async with _CACHE_LOCK:
        _CACHE.pop(bid, None)


def invalidate_cache() -> None:
    """Test hook — wipe the seat cache."""
    _CACHE.clear()


__all__ = [
    "SEATS", "SEAT_COLLECTION",
    "get_current_seat", "set_seat", "invalidate_cache",
]
