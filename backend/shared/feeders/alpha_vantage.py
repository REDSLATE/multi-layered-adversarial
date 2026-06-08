"""Alpha Vantage feeder — DAILY-CACHED fundamentals/news provider.

Doctrine pin (2026-02-XX):
    Alpha Vantage's free tier caps at 25 API calls per UTC day. Any
    consumer that calls AV directly burns quota in seconds and we lose
    AV coverage for the rest of the day. This module is the SINGLE
    EGRESS POINT to alphavantage.co; everything else in the codebase
    that wants AV data MUST call `get_payload()` here.

    Caching strategy:
      * One Mongo row per (symbol, function, date_utc) in
        `alpha_vantage_cache`. The payload is the raw JSON the API
        returned, kept verbatim so consumers stamp evidence references
        replayably.
      * One counter doc per UTC day in `alpha_vantage_quota`. Every
        miss-fetch atomically `$inc`s `count`. When `count` reaches
        the configured cap, the feeder REFUSES the upstream call and
        returns a `quota_exhausted` soft-error to the caller. The
        consumer chooses how to degrade (fall back to a different
        source, surface a notice, etc.).
      * Eviction: the cache is keyed by date string so yesterday's
        rows are naturally orphaned; a TTL pass on every miss prunes
        rows older than `CACHE_RETENTION_DAYS` so the collection
        stays bounded.

    DESCRIPTIVE EVIDENCE ONLY. This feeder writes to read-only
    `alpha_vantage_cache` / `alpha_vantage_quota`. Nothing here
    touches the execution-authority graph.

Configuration (backend/.env):
    ALPHA_VANTAGE_API_KEY          AV API key (required for any
                                   network call; missing → feeder
                                   returns "no_api_key" soft error)
    ALPHA_VANTAGE_DAILY_CAP        per-day call cap; default 25 to
                                   match free tier. Bump if you
                                   upgrade the tier.
    ALPHA_VANTAGE_CACHE_RETENTION  cache retention in days, default 7
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

import httpx

from db import db
from namespaces import ALPHA_VANTAGE_CACHE, ALPHA_VANTAGE_QUOTA
from shared.feeders.feeder_health import record_feeder_health


logger = logging.getLogger("risedual.alpha_vantage")


ALPHA_VANTAGE_BASE_URL = "https://www.alphavantage.co/query"
PROVIDER = "alpha_vantage"

# Default cap matches the free-tier API allowance. Operator can lift
# via `ALPHA_VANTAGE_DAILY_CAP` once they upgrade to a paid plan.
DEFAULT_DAILY_CAP = 25

# Cache retention. AV fundamentals change slowly; 7 days is enough
# for the operator to backfill missing recent days without DOWNTIME.
DEFAULT_CACHE_RETENTION_DAYS = 7

# HTTP timeout — AV is occasionally slow under load.
HTTP_TIMEOUT = httpx.Timeout(connect=5.0, read=20.0, write=10.0, pool=5.0)


def _today_utc() -> str:
    """UTC date bucket key — one slot per calendar day."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _daily_cap() -> int:
    raw = os.environ.get("ALPHA_VANTAGE_DAILY_CAP")
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    return DEFAULT_DAILY_CAP


def _cache_retention_days() -> int:
    raw = os.environ.get("ALPHA_VANTAGE_CACHE_RETENTION")
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    return DEFAULT_CACHE_RETENTION_DAYS


def _api_key() -> Optional[str]:
    k = os.environ.get("ALPHA_VANTAGE_API_KEY", "").strip()
    return k or None


# ──────────────────────── cache ────────────────────────

async def _cache_lookup(symbol: str, function: str, date: str) -> Optional[dict[str, Any]]:
    doc = await db[ALPHA_VANTAGE_CACHE].find_one(
        {"symbol": symbol, "function": function, "date": date},
        {"_id": 0, "payload": 1, "fetched_at": 1},
    )
    return doc or None


async def _cache_store(
    symbol: str, function: str, date: str, payload: dict[str, Any],
) -> None:
    """Idempotent — re-fetching the same (symbol, function, date)
    overwrites the prior payload (operator may force a refetch)."""
    await db[ALPHA_VANTAGE_CACHE].update_one(
        {"symbol": symbol, "function": function, "date": date},
        {
            "$set": {
                "symbol": symbol,
                "function": function,
                "date": date,
                "payload": payload,
                "fetched_at": datetime.now(timezone.utc).isoformat(),
            },
        },
        upsert=True,
    )


async def _cache_prune() -> int:
    """Drop rows older than CACHE_RETENTION_DAYS. Returns count
    deleted. Called opportunistically on each miss-fetch — keeps the
    collection bounded without a separate scheduler.

    Failures here MUST NOT block the live request; a Mongo blip on
    prune is irrelevant to the caller getting their payload."""
    cutoff_date = (
        datetime.now(timezone.utc) - timedelta(days=_cache_retention_days())
    ).strftime("%Y-%m-%d")
    try:
        res = await db[ALPHA_VANTAGE_CACHE].delete_many(
            {"date": {"$lt": cutoff_date}},
        )
        return int(getattr(res, "deleted_count", 0) or 0)
    except Exception:  # noqa: BLE001 — best-effort pruning
        return 0


# ──────────────────────── quota ────────────────────────

async def _quota_state(date: str) -> dict[str, Any]:
    doc = await db[ALPHA_VANTAGE_QUOTA].find_one(
        {"_id": date}, {"count": 1, "first_call_at": 1, "last_call_at": 1},
    )
    return doc or {"_id": date, "count": 0}


async def _quota_increment(date: str) -> int:
    """Atomic increment. Returns the post-increment count."""
    now_iso = datetime.now(timezone.utc).isoformat()
    res = await db[ALPHA_VANTAGE_QUOTA].find_one_and_update(
        {"_id": date},
        {
            "$inc": {"count": 1},
            "$set": {"last_call_at": now_iso},
            "$setOnInsert": {"first_call_at": now_iso},
        },
        upsert=True,
        return_document=True,
    )
    return int((res or {}).get("count", 1))


# ──────────────────────── public surface ────────────────────────

class AVResult(dict):
    """Typed wrapper for clarity. Keys:
        ok: bool                — True iff payload is usable
        payload: dict|None      — raw AV JSON when ok
        from_cache: bool        — True if served from cache (no quota use)
        quota_used_today: int   — post-call counter value
        quota_cap: int          — current cap
        error: str|None         — short reason when not ok
        message: str|None       — human-readable detail
        date: str               — UTC date bucket
    """


def _ok(payload: dict, *, from_cache: bool, count: int, cap: int, date: str) -> AVResult:
    return AVResult(
        ok=True, payload=payload, from_cache=from_cache,
        quota_used_today=count, quota_cap=cap, error=None,
        message=None, date=date,
    )


def _fail(
    *, error: str, message: str, count: int, cap: int, date: str,
) -> AVResult:
    return AVResult(
        ok=False, payload=None, from_cache=False,
        quota_used_today=count, quota_cap=cap, error=error,
        message=message, date=date,
    )


async def get_payload(
    symbol: str,
    function: str,
    *,
    extra_params: Optional[dict[str, Any]] = None,
    force_refresh: bool = False,
) -> AVResult:
    """Single entry point for any AV consumer.

    Behavior:
      1. Hit cache (unless `force_refresh=True`). Cache hit = zero
         quota cost. Returns immediately.
      2. Cache miss → check today's quota. If at cap, return a
         `quota_exhausted` soft error WITHOUT calling the API.
      3. Below cap → POST to AV, persist the JSON to cache, increment
         the quota counter, return the payload.
      4. Network/parse failures are recorded in `feeder_health_audit`
         and surfaced as `error="upstream_error"` so callers can
         degrade. The quota counter is NOT incremented on a failed
         call (we only pay quota for successful payloads).

    Args:
        symbol: e.g. "AAPL". Stored verbatim in the cache key.
        function: AV function name, e.g. "OVERVIEW", "NEWS_SENTIMENT",
            "TIME_SERIES_DAILY". Used as the cache key + AV `function`
            query param.
        extra_params: additional AV query params (e.g. `{"interval": "60min"}`).
        force_refresh: bypass the cache, hit AV directly, overwrite
            the cache. Costs quota even if the row already exists.
    """
    date = _today_utc()
    cap = _daily_cap()

    if not force_refresh:
        cached = await _cache_lookup(symbol, function, date)
        if cached and cached.get("payload"):
            state = await _quota_state(date)
            return _ok(
                cached["payload"], from_cache=True,
                count=int(state.get("count", 0)), cap=cap, date=date,
            )

    # Miss — check quota before reaching for the API.
    state = await _quota_state(date)
    used = int(state.get("count", 0))
    if used >= cap:
        return _fail(
            error="quota_exhausted",
            message=(
                f"alpha_vantage daily cap reached: {used}/{cap} calls today "
                f"({date} UTC). Next reset 00:00 UTC. Consumer should "
                f"degrade gracefully or wait."
            ),
            count=used, cap=cap, date=date,
        )

    key = _api_key()
    if not key:
        return _fail(
            error="no_api_key",
            message="ALPHA_VANTAGE_API_KEY is not set in backend/.env",
            count=used, cap=cap, date=date,
        )

    params = {"function": function, "symbol": symbol, "apikey": key}
    if extra_params:
        for k, v in extra_params.items():
            params[k] = v

    # Best-effort cache pruning so the collection stays bounded. Runs
    # in the background — failures don't block the live request.
    asyncio.create_task(_cache_prune())

    payload: Optional[dict[str, Any]] = None
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            resp = await client.get(ALPHA_VANTAGE_BASE_URL, params=params)
        # AV returns 200 even on validation errors; we check the body.
        if resp.status_code != 200:
            await record_feeder_health(
                provider=PROVIDER, endpoint=function,
                status_code=resp.status_code,
                error_type="http_status_error",
                message=f"AV returned HTTP {resp.status_code}: {resp.text[:200]}",
                context={"symbol": symbol, "function": function},
            )
            return _fail(
                error="upstream_error",
                message=f"AV returned HTTP {resp.status_code}",
                count=used, cap=cap, date=date,
            )
        try:
            payload = resp.json()
        except Exception as e:  # noqa: BLE001
            await record_feeder_health(
                provider=PROVIDER, endpoint=function,
                status_code=resp.status_code,
                error_type="api_error",
                message=f"AV returned non-JSON: {e}",
                context={"symbol": symbol, "function": function},
            )
            return _fail(
                error="upstream_error",
                message="AV returned non-JSON response",
                count=used, cap=cap, date=date,
            )
    except httpx.RequestError as e:
        await record_feeder_health(
            provider=PROVIDER, endpoint=function,
            status_code=None,
            error_type="request_error",
            message=f"AV request failed: {e}",
            context={"symbol": symbol, "function": function},
        )
        return _fail(
            error="upstream_error",
            message=f"AV request failed: {e}",
            count=used, cap=cap, date=date,
        )

    # AV signals throttling via two body markers — handle both.
    if isinstance(payload, dict):
        note = payload.get("Note") or payload.get("Information") or ""
        if isinstance(note, str) and (
            "rate limit" in note.lower()
            or "premium" in note.lower()
            or "thank you for using alpha vantage" in note.lower()
        ):
            await record_feeder_health(
                provider=PROVIDER, endpoint=function,
                status_code=200,
                error_type="rate_limit",
                message=f"AV rate-limit/info body: {note[:200]}",
                context={"symbol": symbol, "function": function},
            )
            # Treat the daily-cap as already hit on our side too —
            # this keeps us from burning more quota in a hot loop.
            # We bump the local counter to the cap so subsequent
            # callers fail-fast without another network round trip.
            await db[ALPHA_VANTAGE_QUOTA].update_one(
                {"_id": date},
                {"$set": {"count": cap, "last_call_at": datetime.now(timezone.utc).isoformat()}},
                upsert=True,
            )
            return _fail(
                error="quota_exhausted",
                message="AV signalled rate-limit/premium-required",
                count=cap, cap=cap, date=date,
            )
        if "Error Message" in payload:
            await record_feeder_health(
                provider=PROVIDER, endpoint=function,
                status_code=200,
                error_type="api_error",
                message=f"AV Error Message: {payload['Error Message'][:200]}",
                context={"symbol": symbol, "function": function},
            )
            return _fail(
                error="upstream_error",
                message=str(payload["Error Message"])[:300],
                count=used, cap=cap, date=date,
            )

    # Success path — store the payload, bump the quota counter.
    await _cache_store(symbol, function, date, payload or {})
    new_count = await _quota_increment(date)
    return _ok(
        payload or {}, from_cache=False,
        count=new_count, cap=cap, date=date,
    )


async def quota_state(date: Optional[str] = None) -> dict[str, Any]:
    """Operator-facing view of today's (or a specific day's) quota."""
    d = date or _today_utc()
    state = await _quota_state(d)
    cap = _daily_cap()
    used = int(state.get("count", 0))
    return {
        "date": d,
        "used": used,
        "cap": cap,
        "remaining": max(0, cap - used),
        "first_call_at": state.get("first_call_at"),
        "last_call_at": state.get("last_call_at"),
    }


__all__ = ["get_payload", "quota_state", "AVResult", "PROVIDER"]
