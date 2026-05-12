"""Per-tier rate limits for /api/public/*.

Sliding-fixed-window: one Mongo doc per `(tier, minute-bucket)`, atomic
`$inc` on every request. On the 1st request of a new minute the doc is
upserted with `count=1`; subsequent requests in the same minute bump
the counter and compare against the tier's cap.

Defaults (per minute):
    free       30
    starter    60
    pro       300
    pro_max  1200
    unknown    20  ← belt-and-suspenders for misrouted callers

Overrideable via env vars (also per-minute):
    RATE_LIMIT_FREE_PER_MIN, RATE_LIMIT_STARTER_PER_MIN,
    RATE_LIMIT_PRO_PER_MIN, RATE_LIMIT_PRO_MAX_PER_MIN.

When exceeded, MC returns HTTP 429 with `Retry-After: <seconds>` and a
JSON body explaining which tier hit which limit. This integrates with
the public-traffic page automatically — 429 rows appear in the tail
log so the operator can see the cap being hit in real time.

This module is fail-OPEN: if Mongo hiccups, we let the request through.
The point of rate-limiting here is to protect MC from a runaway proxy
loop on risedual.ai's side, not to be a hardened DoS shield.
"""
from __future__ import annotations

import os
import time
from typing import Optional

from fastapi import HTTPException, Request

from db import db
from namespaces import PUBLIC_RATE_LIMITS


# ──────────────────────── defaults ────────────────────────

DEFAULTS_PER_MIN = {
    "free": 30,
    "starter": 60,
    "pro": 300,
    "pro_max": 1200,
}
UNKNOWN_TIER_CAP = 20

_ENV_OVERRIDES = {
    "free": "RATE_LIMIT_FREE_PER_MIN",
    "starter": "RATE_LIMIT_STARTER_PER_MIN",
    "pro": "RATE_LIMIT_PRO_PER_MIN",
    "pro_max": "RATE_LIMIT_PRO_MAX_PER_MIN",
}


def tier_limit(tier: str) -> int:
    env_key = _ENV_OVERRIDES.get(tier)
    if env_key and (val := os.environ.get(env_key)):
        try:
            return max(1, int(val))
        except ValueError:
            pass
    return DEFAULTS_PER_MIN.get(tier, UNKNOWN_TIER_CAP)


def _minute_bucket() -> str:
    """yyyy-mm-ddTHH:MM bucket key — one slot per wall-clock minute."""
    return time.strftime("%Y-%m-%dT%H:%M", time.gmtime())


# ──────────────────────── enforcement ────────────────────────

async def check_and_consume(tier: str) -> tuple[int, int]:
    """Atomically increment + return (count_after, cap).
    Raises HTTPException(429) if the cap is exceeded.
    Fails OPEN on Mongo errors — never blocks because logging broke.
    """
    cap = tier_limit(tier)
    bucket = _minute_bucket()
    key = f"{tier}:{bucket}"
    now = int(time.time())

    try:
        res = await db[PUBLIC_RATE_LIMITS].find_one_and_update(
            {"_id": key},
            {
                "$inc": {"count": 1},
                "$setOnInsert": {
                    "tier": tier,
                    "bucket": bucket,
                    # `expire_at` carries the doc into the TTL index so
                    # we don't leave dead rows behind.
                    "expire_at_epoch": now + 120,
                },
            },
            upsert=True,
            return_document=True,  # pymongo 4.x: returns post-update doc
        )
    except Exception:  # noqa: BLE001
        # Fail open — DB hiccup must not become a 5xx for callers.
        return 0, cap

    count = int((res or {}).get("count", 1))
    if count > cap:
        # Seconds remaining in the current minute bucket.
        retry_after = max(1, 60 - int(time.time()) % 60)
        raise HTTPException(
            status_code=429,
            detail=(
                f"rate limit exceeded for tier={tier}: "
                f"{count}/{cap} requests in current minute"
            ),
            headers={
                "Retry-After": str(retry_after),
                "X-RateLimit-Tier": tier,
                "X-RateLimit-Limit": str(cap),
                "X-RateLimit-Window": "60",
                "X-RateLimit-Remaining": "0",
            },
        )
    return count, cap


async def ensure_ttl_index() -> None:
    """Create the TTL index once. Idempotent."""
    try:
        await db[PUBLIC_RATE_LIMITS].create_index(
            "expire_at_epoch", expireAfterSeconds=0,
        )
    except Exception:  # noqa: BLE001
        # If index creation fails (e.g., already exists with conflicting
        # options), don't crash the app — buckets are tiny anyway.
        pass


# ──────────────────────── middleware ────────────────────────

PUBLIC_PREFIX = "/api/public/"

# Endpoints we WANT to rate-limit. Chat and narrative are LLM-expensive
# so we rate-limit them. Everything else is read-from-mongo and very
# cheap. We keep the cap broad rather than per-endpoint for v1.

# Health-check-style paths inside /api/public/* (none today; keep slot
# for future) can be excluded here.
RATE_LIMIT_EXCLUDED_PATHS: frozenset[str] = frozenset()


async def rate_limit_middleware(request: Request, call_next):
    """Mounted globally. Enforces tier-aware rate limits on /api/public/*.

    Runs BEFORE `public_traffic_middleware` writes the row, so a 429
    still gets logged with its real status. We trust the
    `X-RiseDual-User-Tier` header here — anyone hitting MC without a
    valid `X-RiseDual-Token` will already be 401'd by the dep, so the
    only callers reaching this point are trust-validated. Misspellings
    fall to the conservative `unknown` cap.
    """
    path = request.url.path
    if not path.startswith(PUBLIC_PREFIX) or path in RATE_LIMIT_EXCLUDED_PATHS:
        return await call_next(request)

    # We need to enforce the limit BEFORE the request is processed, but
    # we also don't want to trip the limit for requests that will
    # immediately get rejected by the trust dep (401/422). Cheap
    # heuristic: if the trust token is missing, skip the rate-limit
    # increment — the request will 401 in the dep anyway.
    tok = request.headers.get("X-RiseDual-Token")
    if not tok:
        return await call_next(request)

    tier = (request.headers.get("X-RiseDual-User-Tier") or "free").strip().lower()
    # Normalize unknown tier values to a sentinel so the cap applies
    # without leaking arbitrary strings into Mongo keys.
    if tier not in DEFAULTS_PER_MIN:
        tier = "unknown"

    try:
        count, cap = await check_and_consume(tier)
    except HTTPException as e:
        # Format the body as JSON so the public-traffic logger captures
        # a clean response (FastAPI's default exception handler returns
        # the right shape).
        from fastapi.responses import JSONResponse
        return JSONResponse(
            status_code=e.status_code,
            content={"detail": e.detail},
            headers=e.headers or {},
        )

    response = await call_next(request)
    # Stamp rate-limit headers so risedual.ai's backend can surface
    # remaining quota client-side.
    cap_val = tier_limit(tier)
    response.headers["X-RateLimit-Tier"] = tier
    response.headers["X-RateLimit-Limit"] = str(cap_val)
    response.headers["X-RateLimit-Remaining"] = str(max(0, cap_val - count))
    response.headers["X-RateLimit-Window"] = "60"
    return response


__all__ = [
    "DEFAULTS_PER_MIN", "UNKNOWN_TIER_CAP",
    "check_and_consume", "ensure_ttl_index",
    "rate_limit_middleware", "tier_limit",
]
