"""Kraken per-pair notional floor (2026-02-17).

Emerged from the 2026-02-17 clearance funnel: 91 crypto intents/hour
dying at Kraken's `EGeneral:Invalid arguments:volume minimum not met`
rejection because the auto-router sized orders below Kraken's per-pair
`ordermin` (in units of the base coin).

Doctrine (operator-set 2026-02-17):
    Per-pair floor is expressed in USD notional (operator-native units).
    If an intent's post-risk notional falls below the pair's floor:
        policy=size_up  → notional is raised UP to the floor
                          (default — maximize throughput)
        policy=reject   → intent is terminated with reason
                          `notional_below_pair_floor`
    Unknown pairs fall back to `KRAKEN_DEFAULT_MIN_NOTIONAL_USD` env
    (default 5.0). Pairs with `min_notional_usd=0` are explicitly
    UNGATED — nothing changes for them.

Storage: `kraken_pair_floors` collection, one doc per pair. Doc shape:
    {
        _id:                "BTC/USD",
        min_notional_usd:    5.0,
        policy:              "size_up" | "reject",
        updated_at:          iso8601,
        updated_by:          operator email,
        notes:               free-form string (optional),
    }

Runtime cache: read-through with a small in-process LRU + TTL so the
auto-router doesn't hit Mongo on every tick. Cache is invalidated
implicitly by the 30-second TTL; explicit invalidation is available
via `invalidate_cache()` for the route handlers that mutate floors.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Optional

from db import db


logger = logging.getLogger("kraken_pair_floors")

COLLECTION = "kraken_pair_floors"

# Fallback floor for pairs the operator hasn't explicitly configured.
# 5.0 USD is a reasonable start for most Kraken pairs (their ordermin
# × current price is usually well under $5 for USD-quoted pairs). If a
# pair has an unusually high ordermin, the operator overrides it here.
DEFAULT_MIN_NOTIONAL_USD = float(os.environ.get("KRAKEN_DEFAULT_MIN_NOTIONAL_USD", "5.0"))

_POLICY_SIZE_UP = "size_up"
_POLICY_REJECT = "reject"
ALLOWED_POLICIES = {_POLICY_SIZE_UP, _POLICY_REJECT}

_CACHE_TTL_S = 30.0
_cache: dict = {}
_cache_ts: float = 0.0


@dataclass(frozen=True)
class PairFloor:
    pair: str
    min_notional_usd: float
    policy: str  # size_up | reject
    is_default: bool  # True when the value came from the env default


@dataclass(frozen=True)
class FloorApplyResult:
    """Return from `apply_floor(intent_notional, pair)` — the auto-router
    reads `.notional_usd` if `.allowed` is True; otherwise it terminates
    the intent with reason `.reject_reason`."""
    allowed: bool
    notional_usd: float
    reject_reason: Optional[str]
    adjusted: bool          # True when policy=size_up actually raised the notional
    original_notional: float
    floor: PairFloor


async def _load_all() -> dict[str, dict]:
    """Read every pair-floor doc from Mongo. Called from the cache
    layer only."""
    out: dict[str, dict] = {}
    async for d in db[COLLECTION].find({}):
        pair = d.get("_id")
        if not pair:
            continue
        out[str(pair)] = d
    return out


async def _fresh_cache() -> dict[str, dict]:
    """Return the pair-floor map, refreshing from Mongo if stale."""
    global _cache, _cache_ts  # noqa: PLW0603
    if time.time() - _cache_ts < _CACHE_TTL_S and _cache is not None:
        return _cache
    _cache = await _load_all()
    _cache_ts = time.time()
    return _cache


def invalidate_cache() -> None:
    """Force the next `get_floor` / `apply_floor` call to refetch. Route
    handlers that mutate floors MUST call this so the auto-router's
    next tick sees the change."""
    global _cache_ts  # noqa: PLW0603
    _cache_ts = 0.0


async def get_floor(pair: str) -> PairFloor:
    """Return the effective floor for `pair`. Falls back to the env
    default if no explicit config exists."""
    cache = await _fresh_cache()
    doc = cache.get(pair)
    if doc:
        return PairFloor(
            pair=pair,
            min_notional_usd=float(doc.get("min_notional_usd") or 0.0),
            policy=str(doc.get("policy") or _POLICY_SIZE_UP),
            is_default=False,
        )
    return PairFloor(
        pair=pair,
        min_notional_usd=DEFAULT_MIN_NOTIONAL_USD,
        policy=_POLICY_SIZE_UP,
        is_default=True,
    )


async def apply_floor(pair: str, notional_usd: float) -> FloorApplyResult:
    """Apply the pair's floor to `notional_usd`. See doctrine at the
    top of the module for policy semantics.

    Callers should treat `result.allowed==False` as terminal — the
    intent must be blocked with `result.reject_reason` (which is set
    only when policy=reject and notional was below the floor).
    """
    floor = await get_floor(pair)

    # `min_notional_usd == 0` is the explicit "ungated" signal —
    # honor operator intent, never adjust.
    if floor.min_notional_usd <= 0:
        return FloorApplyResult(
            allowed=True,
            notional_usd=notional_usd,
            reject_reason=None,
            adjusted=False,
            original_notional=notional_usd,
            floor=floor,
        )

    if notional_usd >= floor.min_notional_usd:
        return FloorApplyResult(
            allowed=True,
            notional_usd=notional_usd,
            reject_reason=None,
            adjusted=False,
            original_notional=notional_usd,
            floor=floor,
        )

    if floor.policy == _POLICY_REJECT:
        return FloorApplyResult(
            allowed=False,
            notional_usd=notional_usd,
            reject_reason=(
                f"notional_below_pair_floor: "
                f"${notional_usd:.4f} < ${floor.min_notional_usd:.4f} for {pair}"
            ),
            adjusted=False,
            original_notional=notional_usd,
            floor=floor,
        )

    # policy=size_up (the default) — raise notional to the floor.
    return FloorApplyResult(
        allowed=True,
        notional_usd=floor.min_notional_usd,
        reject_reason=None,
        adjusted=True,
        original_notional=notional_usd,
        floor=floor,
    )


__all__ = [
    "COLLECTION",
    "DEFAULT_MIN_NOTIONAL_USD",
    "ALLOWED_POLICIES",
    "PairFloor",
    "FloorApplyResult",
    "get_floor",
    "apply_floor",
    "invalidate_cache",
]
