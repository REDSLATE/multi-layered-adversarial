"""Symbol Registry — canonical-universe ↔ broker-symbol resolver.

Doctrine (2026-07-15, iter-30 P3, operator directive):

    "Don't make Webull define your universe. Instead:

        Canonical Universe → Adapter Symbol Registry → Webull / Kraken /
        Future Brokers…

    That way, if you later add another broker, you only update the
    registry instead of changing every brain or strategy."

Every broker adapter that needs to talk to a broker about a symbol
consults this module first. On a cache HIT (the symbol was resolved
before its TTL expired) the adapter reuses the previously-stamped
`instrument_id` + `tradable` verdict and never touches the broker
API. On a cache MISS the adapter probes the broker, stamps the
result into `symbol_registry`, and returns it.

Why this exists
---------------
Without a registry, every module that needs a Webull instrument_id
runs its own ad-hoc lookup. When Webull rejects a symbol (e.g. HOTH
returning `INVALID_SYMBOL` because the free entitlement doesn't
cover it), the failure repeats FOREVER — every pulse tick burns
another Webull budget slot on a symbol the broker will never
resolve. The circuit breaker eventually trips, and now a symbol
the broker DOES support (say, NVDA) is starved for 60 seconds
because of one bad neighbor.

The registry solves this by CACHING the negative resolution — once
we learn Webull says "no" to HOTH, we stop asking for the TTL
window (default 6h). Adapters treat `tradable=False` as a fast
short-circuit to the same `None`/skip semantic they already have
for missing quotes, so no upstream doctrine change is needed.

Shape
-----
Collection: `symbol_registry`
    _id       : canonical symbol (e.g. "HOTH", "BTC/USD")
    brokers   : {
        webull: {
            instrument_id : Optional[str],
            tradable      : bool,
            reason        : Optional[str],   # "resolved" | "invalid_symbol"
                                             # | "http_error" | "circuit_open"
            resolved_at   : ISO-8601 UTC,
            expires_at    : ISO-8601 UTC,    # after this we re-probe
        },
        # kraken, future brokers, …
    }
    updated_at : ISO-8601 UTC

Fail-soft
---------
Every function catches DB errors internally and returns a safe
default (usually `None` / `False`). A Mongo outage MUST NOT prevent
the trading loop from making a best-effort decision.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, TypedDict

from db import db
from namespaces import SYMBOL_REGISTRY

logger = logging.getLogger("risedual.broker.symbol_registry")

# Default TTL: 6 hours. Symbols the broker rejects get re-probed
# daily-ish so a delisted-then-relisted ticker isn't permanently
# locked out. Symbols the broker resolves get the same 6h TTL — if
# Webull changes their internal `instrument_id` for a ticker, we
# want to pick up the new one within a session.
_DEFAULT_TTL_SEC = 6 * 3600


class SymbolResolution(TypedDict, total=False):
    """Adapter-facing view of a registry row."""
    canonical_symbol: str
    broker: str
    instrument_id: Optional[str]
    tradable: bool
    reason: Optional[str]
    resolved_at: Optional[str]
    from_cache: bool


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None


async def get_cached(
    broker: str, canonical_symbol: str,
) -> Optional[SymbolResolution]:
    """Return the cached resolution for `(broker, canonical_symbol)`,
    or None if there is no row OR the row's TTL has expired.

    Callers use this on the fast path — if it returns a hit, they
    skip the broker probe entirely.
    """
    sym = (canonical_symbol or "").upper().strip()
    if not sym or not broker:
        return None
    try:
        doc = await db[SYMBOL_REGISTRY].find_one(
            {"_id": sym}, {"brokers": 1},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "symbol_registry get_cached failed sym=%s broker=%s err=%s",
            sym, broker, exc,
        )
        return None
    if not doc:
        return None
    row = ((doc.get("brokers") or {}).get(broker)) or None
    if not row:
        return None
    expires = _parse_iso(row.get("expires_at"))
    if expires is None:
        return None
    # Compare in UTC — Mongo strings are ISO-8601 with tz.
    if _now_utc() >= expires:
        # Expired — behave as if there's no cache entry so the
        # caller re-probes. The stale row stays in Mongo until it's
        # overwritten; that's cheaper than a targeted $unset and
        # keeps a small forensic trail of the last verdict.
        return None
    return {
        "canonical_symbol": sym,
        "broker": broker,
        "instrument_id": row.get("instrument_id"),
        "tradable": bool(row.get("tradable")),
        "reason": row.get("reason"),
        "resolved_at": row.get("resolved_at"),
        "from_cache": True,
    }


async def stamp(
    broker: str,
    canonical_symbol: str,
    *,
    instrument_id: Optional[str],
    tradable: bool,
    reason: Optional[str] = None,
    ttl_seconds: int = _DEFAULT_TTL_SEC,
) -> None:
    """Write a resolution into the registry.

    Idempotent: subsequent calls for the same `(broker, symbol)`
    overwrite the sub-doc. Fail-soft — a DB error is logged and
    swallowed; the adapter keeps the resolution in memory for this
    process even if we couldn't persist it.
    """
    sym = (canonical_symbol or "").upper().strip()
    if not sym or not broker:
        return
    now = _now_utc()
    expires = now + timedelta(seconds=max(60, int(ttl_seconds)))
    sub = {
        "instrument_id": instrument_id,
        "tradable": bool(tradable),
        "reason": reason or ("resolved" if tradable else "unresolved"),
        "resolved_at": _iso(now),
        "expires_at": _iso(expires),
    }
    try:
        await db[SYMBOL_REGISTRY].update_one(
            {"_id": sym},
            {
                "$set": {
                    f"brokers.{broker}": sub,
                    "updated_at": _iso(now),
                },
                "$setOnInsert": {
                    "_id": sym,
                    "first_seen_at": _iso(now),
                },
            },
            upsert=True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "symbol_registry stamp failed sym=%s broker=%s err=%s",
            sym, broker, exc,
        )


async def mark_unsupported(
    broker: str, canonical_symbol: str, reason: str = "invalid_symbol",
    *,
    ttl_seconds: int = _DEFAULT_TTL_SEC,
) -> None:
    """Convenience: stamp a symbol as NOT tradable on this broker.

    Adapters call this after the broker returns `INVALID_SYMBOL` or
    any other terminal "no" verdict. The next `resolve` call within
    the TTL will return `tradable=False` without touching the
    broker API.
    """
    await stamp(
        broker,
        canonical_symbol,
        instrument_id=None,
        tradable=False,
        reason=reason,
        ttl_seconds=ttl_seconds,
    )


async def resolve(
    broker: str,
    canonical_symbol: str,
    *,
    probe_fn=None,
    ttl_seconds: int = _DEFAULT_TTL_SEC,
) -> SymbolResolution:
    """Resolve `canonical_symbol` for `broker`. Returns a
    `SymbolResolution` describing the current best answer.

    Behaviour:
      1. Check the Mongo cache. If non-expired, return it (marked
         `from_cache=True`).
      2. Otherwise call `probe_fn(canonical_symbol)` — the adapter-
         supplied callback that hits the broker's instrument-lookup
         API. `probe_fn` returns either:
             * `dict(instrument_id=..., tradable=True)` on success
             * `dict(tradable=False, reason=...)` on a known-terminal
                broker "no"
             * `None` if the probe itself couldn't complete (network
                error / circuit-breaker open) — in which case the
                registry stays untouched so we re-probe next call.
      3. On a completed probe, stamp the result and return it.

    `probe_fn` is the adapter's business — this module never imports
    from an adapter. Inversion keeps the registry side-effect-free
    beyond its own Mongo collection.
    """
    sym = (canonical_symbol or "").upper().strip()
    if not sym or not broker:
        return {
            "canonical_symbol": sym,
            "broker": broker,
            "instrument_id": None,
            "tradable": False,
            "reason": "empty_symbol",
            "resolved_at": None,
            "from_cache": False,
        }

    cached = await get_cached(broker, sym)
    if cached is not None:
        return cached

    if probe_fn is None:
        return {
            "canonical_symbol": sym,
            "broker": broker,
            "instrument_id": None,
            "tradable": False,
            "reason": "no_probe_fn",
            "resolved_at": None,
            "from_cache": False,
        }

    try:
        probe = await probe_fn(sym) if _is_awaitable_fn(probe_fn) else probe_fn(sym)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "symbol_registry probe raised sym=%s broker=%s err=%s",
            sym, broker, exc,
        )
        return {
            "canonical_symbol": sym,
            "broker": broker,
            "instrument_id": None,
            "tradable": False,
            "reason": "probe_error",
            "resolved_at": None,
            "from_cache": False,
        }

    if probe is None:
        # Probe couldn't complete cleanly (e.g. broker unreachable).
        # Don't poison the cache — let the next tick try again.
        return {
            "canonical_symbol": sym,
            "broker": broker,
            "instrument_id": None,
            "tradable": False,
            "reason": "probe_incomplete",
            "resolved_at": None,
            "from_cache": False,
        }

    instrument_id = probe.get("instrument_id")
    tradable = bool(probe.get("tradable"))
    reason = probe.get("reason") or ("resolved" if tradable else "unresolved")
    await stamp(
        broker, sym,
        instrument_id=instrument_id,
        tradable=tradable,
        reason=reason,
        ttl_seconds=ttl_seconds,
    )
    return {
        "canonical_symbol": sym,
        "broker": broker,
        "instrument_id": instrument_id,
        "tradable": tradable,
        "reason": reason,
        "resolved_at": _iso(_now_utc()),
        "from_cache": False,
    }


def _is_awaitable_fn(fn) -> bool:
    """Detect coroutine functions without importing inspect at module
    top — keeps import cost tiny for the hot path."""
    import inspect
    return inspect.iscoroutinefunction(fn)


async def registry_snapshot(broker: Optional[str] = None) -> list[dict]:
    """Diagnostic accessor for a future admin tile — return every
    row (or every row for one broker) so the operator can eyeball
    what the registry has learned.

    Best-effort: swallows DB errors and returns [] rather than
    raising. This is a read-only view, no side effects.
    """
    query: dict = {}
    if broker:
        query[f"brokers.{broker}"] = {"$exists": True}
    projection = {"_id": 1, "brokers": 1, "updated_at": 1}
    try:
        cur = db[SYMBOL_REGISTRY].find(query, projection).sort(
            "updated_at", -1,
        ).limit(500)
        return [d async for d in cur]
    except Exception as exc:  # noqa: BLE001
        logger.warning("symbol_registry snapshot read failed: %s", exc)
        return []
