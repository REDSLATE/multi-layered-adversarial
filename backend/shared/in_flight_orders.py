"""In-flight order dedupe ledger.

Doctrine pin (operator directive, 2026-06-10, post-AAPL incident):

    On 2026-06-09 MC fired 130 BUYs on AAPL in 13 minutes. The
    proximate cause was the gap between "we submitted an order" and
    "Public.com indexed the fill." The auto-router's pickup cache
    saw FLAT every pass because the broker round-trip had finished
    but its truth hadn't propagated to MC's position context yet.

    This module closes that window. The auto-router records every
    symbol it has just submitted an order for, the instant it
    submits, BEFORE any broker round-trip. Until that entry ages
    out (default 30s) the auto-router refuses to submit another
    order for the same symbol.

    Combined with `shared.broker_fills.has_pending_order` — which
    answers "did Public.com index a fill within the last 30s?" —
    these two together cover both halves of the propagation
    window:
        * pre-broker-ack  → in-memory pending set (this module)
        * post-broker-ack → shared_broker_fills (already built)

    Doctrine constraint: only ONE order per symbol can be in flight
    at any given moment, full stop. The auto-router does not size
    "partial top-ups" — that's a planner concern and is intentionally
    out of scope here.

Implementation:
    * Lives in-process, in-memory. State is lost on pod restart —
      but the broker-fills oracle picks up there immediately.
    * Lock-protected so concurrent _tick() passes can't race.
    * `_PENDING_TTL_SEC` is the age-out window. Tunable via env.
"""
from __future__ import annotations

import asyncio
import os
import time
from typing import Optional


_PENDING_TTL_SEC: int = int(os.environ.get("IN_FLIGHT_ORDER_TTL_SEC", "30"))

# symbol -> claim record
# {"ts_monotonic": float, "ts_iso": str, "intent_id": str | None}
_pending: dict[str, dict] = {}
_lock = asyncio.Lock()


def _prune_locked() -> None:
    """Drop entries whose monotonic-age exceeds the TTL.

    Caller MUST hold `_lock`. Cheap O(n) sweep — `n` is bounded by
    the number of distinct symbols actively trading, which in
    practice is < 20.
    """
    now = time.monotonic()
    expired = [
        k for k, v in _pending.items()
        if (now - v["ts_monotonic"]) > _PENDING_TTL_SEC
    ]
    for k in expired:
        _pending.pop(k, None)


async def claim_in_flight_slot(
    symbol: str,
    *,
    intent_id: Optional[str] = None,
) -> bool:
    """Atomically reserve the in-flight slot for `symbol`.

    Returns True iff the caller successfully claimed the slot —
    they may proceed to submit the order to the broker.

    Returns False if another order is already in flight for this
    symbol. Caller MUST NOT submit; this is the dedupe block.
    """
    sym = (symbol or "").upper()
    if not sym:
        return False
    async with _lock:
        _prune_locked()
        if sym in _pending:
            return False
        _pending[sym] = {
            "ts_monotonic": time.monotonic(),
            "ts_iso": _now_iso(),
            "intent_id": intent_id,
        }
        return True


async def release_in_flight_slot(symbol: str) -> None:
    """Release a previously-claimed slot.

    Called when the broker outright rejects the submission, or when
    the auto-router itself fails before the broker round-trip
    completes. Successful fills do NOT release — the broker-fills
    oracle takes over enforcement at that point and the in-memory
    entry simply ages out.
    """
    sym = (symbol or "").upper()
    if not sym:
        return
    async with _lock:
        _pending.pop(sym, None)


async def is_in_flight(symbol: str) -> bool:
    """Read-only check — is this symbol currently locked?

    Mainly used by tests and the admin route. The auto-router itself
    uses `claim_in_flight_slot` so the check-and-set is atomic.
    """
    sym = (symbol or "").upper()
    if not sym:
        return False
    async with _lock:
        _prune_locked()
        return sym in _pending


def snapshot() -> dict:
    """Diagnostic snapshot. Synchronous: callers (e.g. admin route)
    just want a frozen view of the current state."""
    now = time.monotonic()
    # Read-only sweep — no lock needed because dict ops are atomic in
    # CPython at the granularity of single operations and we tolerate
    # a torn read for a diagnostic.
    items = list(_pending.items())
    out = []
    for sym, v in items:
        age = now - v["ts_monotonic"]
        if age > _PENDING_TTL_SEC:
            continue
        out.append({
            "symbol": sym,
            "age_seconds": round(age, 2),
            "claimed_at": v.get("ts_iso"),
            "intent_id": v.get("intent_id"),
        })
    return {
        "ttl_seconds": _PENDING_TTL_SEC,
        "count": len(out),
        "pending": out,
    }


def reset_for_tests() -> None:
    """Drop all state. Test-only."""
    _pending.clear()


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "claim_in_flight_slot",
    "release_in_flight_slot",
    "is_in_flight",
    "snapshot",
    "reset_for_tests",
]
