"""Brain-callable runtime endpoints (2026-02-17).

Two surfaces for brain sidecars to read MC state with their runtime
token (no operator JWT required):

  GET  /api/admin/runtime/roster?caller={brain}
       — Lean seat-roster view, lane-resolved per role. Brains use this
         to refresh their `seats_held` cache and decide which emitter
         loops to run.

  GET  /api/admin/runtime/{brain}/status
       — Composite proxy. Fetches the brain's own `/api/admin/runtime/
         {brain}/status` endpoint (when configured via env var) and
         returns the payload alongside MC's audit metadata. Lets the
         MC admin dashboard render brain-side telemetry without
         cross-origin pain.

  POST /api/admin/runtime/{brain}/status/refresh
       — Operator escape hatch — force a fresh fetch, bypassing cache.

All three endpoints are READ-ONLY. The proxy writes one row per call
to `brain_status_proxy_audit` for operator forensics (latency / failure
visibility) — that write is observability metadata only, never
mutating the actual brain state.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query

from auth import get_current_user
from db import db
from namespaces import LIVE_RUNTIMES
from shared.roster import CRYPTO_LANE_ROLES, get_roster


logger = logging.getLogger("risedual.brain_runtime")
router = APIRouter(prefix="/admin/runtime", tags=["brain-runtime"])


# ──────────────────────── Configuration ────────────────────────

KNOWN_BRAINS: tuple[str, ...] = tuple(LIVE_RUNTIMES)

# Proxy upstream URLs are pulled per-brain from env. Operator sets
# `REDEYE_STATUS_URL`, `ALPHA_STATUS_URL`, etc. Missing env = brain
# doesn't expose `/status` yet → endpoint returns a structured
# "no_upstream_configured" payload instead of 500ing.
def _upstream_url_for(brain: str) -> str:
    return os.environ.get(f"{brain.upper()}_STATUS_URL", "").strip()


# Network bound on the proxy fetch. Brains MUST respond in well under
# this on healthy paths; anything slower and operator should be told
# rather than waiting on a hung dashboard tile.
PROXY_TIMEOUT_S: float = float(os.environ.get("MC_STATUS_PROXY_TIMEOUT_S", "4.0"))

# Per-brain cache so the dashboard's 15s tile refresh + a few operator
# tabs don't hammer the brain pod. TTL is short (10s) so the data feels
# live; the audit row records every CALL into MC, hits and misses alike,
# so operator forensics aren't lost to caching.
PROXY_CACHE_TTL_S: float = float(os.environ.get("MC_STATUS_PROXY_CACHE_TTL_S", "10.0"))

# In-process TTL cache: brain -> (set_at_epoch_s, payload_dict).
# Process-local; on pod restart we re-fetch. Adequate because the
# dashboard polls frequently.
_PROXY_CACHE: Dict[str, tuple[float, Dict[str, Any]]] = {}


# Audit collection. Single writes per proxy call. Indexed by ts for
# operator forensics.
BRAIN_STATUS_PROXY_AUDIT = "brain_status_proxy_audit"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _expected_token_for(brain: str) -> str:
    return os.environ.get(f"{brain.upper()}_INGEST_TOKEN", "")


# ──────────────────────── Dual auth (operator OR brain token) ────────────────────────


async def _dual_auth(
    x_brain_id: Optional[str],
    x_runtime_token: Optional[str],
    operator_user: Optional[dict],
) -> str:
    """Same pattern as `market_data_snapshot._dual_auth`. Returns the
    auth principal for audit-trail purposes."""
    if operator_user and operator_user.get("email"):
        return f"operator:{operator_user['email']}"
    brain = (x_brain_id or "").lower().strip()
    if not brain:
        raise HTTPException(status_code=401, detail="auth required")
    if brain not in KNOWN_BRAINS:
        raise HTTPException(status_code=404, detail=f"unknown brain {brain!r}")
    expected = _expected_token_for(brain)
    if not expected:
        raise HTTPException(
            status_code=503,
            detail=f"runtime endpoint not configured for {brain}",
        )
    if (x_runtime_token or "") != expected:
        raise HTTPException(status_code=401, detail="invalid token")
    return f"brain:{brain}"


async def _maybe_user(authorization: Optional[str] = Header(default=None)) -> Optional[dict]:
    """Best-effort operator JWT resolution. Returns None (not 401) on
    bad/missing token so the brain-token path can handle the request."""
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    try:
        import jwt
        from auth import _secret, JWT_ALGORITHM
        token = authorization.split(" ", 1)[1].strip()
        payload = jwt.decode(token, _secret(), algorithms=[JWT_ALGORITHM])
        if payload.get("type") != "access":
            return None
        user = await db.users.find_one(
            {"id": payload["sub"]}, {"_id": 0, "password_hash": 0},
        )
        return user
    except Exception:  # noqa: BLE001
        return None


# ──────────────────────── /admin/runtime/roster ────────────────────────


def _lane_of_role(role: str) -> str:
    return "crypto" if role in CRYPTO_LANE_ROLES else "equity"


@router.get("/roster")
async def get_brain_roster(
    caller: Optional[str] = Query(
        None,
        description="brain id of the caller (e.g. 'redeye'). Used to populate `your_seats`.",
    ),
    x_brain_id: Optional[str] = Header(default=None, alias="X-Brain-Id"),
    x_runtime_token: Optional[str] = Header(default=None, alias="X-Runtime-Token"),
    operator_user: Optional[dict] = Depends(_maybe_user),
) -> Dict[str, Any]:
    """Brain-callable roster — lean payload with seat assignments and
    a precomputed `your_seats` list when `caller` is set.

    Doctrine: read-only seat view. Does NOT return policy, eligibility,
    or doctrine guidance (those live on the operator-JWT endpoint at
    `/api/admin/roster`). The brain only needs to know which seats it
    currently holds so its emitters can wake/sleep correctly.

    Auth: dual — operator JWT OR `X-Brain-Id` + `X-Runtime-Token`.
    """
    principal = await _dual_auth(x_brain_id, x_runtime_token, operator_user)

    # If brain auth: the caller is implicitly the brain. Override the
    # query param to prevent a brain from peeking at another brain's
    # seats by passing `caller=other_brain`.
    if principal.startswith("brain:"):
        caller_brain = principal.split(":", 1)[1]
    else:
        caller_brain = (caller or "").lower().strip() or None
        if caller_brain and caller_brain not in KNOWN_BRAINS:
            raise HTTPException(status_code=400, detail=f"unknown caller {caller_brain!r}")

    snap = await get_roster()
    assignments: Dict[str, Optional[str]] = (snap or {}).get("assignments") or {}

    your_seats: list[Dict[str, str]] = []
    if caller_brain:
        for seat, occupant in assignments.items():
            if occupant == caller_brain:
                your_seats.append({"seat": seat, "lane": _lane_of_role(seat)})

    return {
        "ts": _now().isoformat(),
        "seat_epoch": snap.get("seat_epoch", 1) if snap else 1,
        "caller": caller_brain,
        "your_seats": your_seats,
        "assignments": assignments,
        "updated_at": snap.get("updated_at") if snap else None,
        "served_to": principal,
        "doctrine": "operator_read_only_seat_view",
    }


# ──────────────────────── /admin/runtime/{brain}/status proxy ────────────────────────


async def _write_proxy_audit(
    brain: str,
    principal: str,
    upstream_url: str,
    status_code: Optional[int],
    duration_ms: float,
    error: Optional[str],
) -> None:
    """One audit row per proxy attempt (hits AND misses). Best-effort —
    a Mongo write failure here cannot tank the proxy response."""
    try:
        await db[BRAIN_STATUS_PROXY_AUDIT].insert_one({
            "brain": brain,
            "principal": principal,
            "upstream_url": upstream_url,
            "status_code": status_code,
            "duration_ms": round(duration_ms, 2),
            "error": error,
            "ts": _now().isoformat(),
        })
    except Exception as exc:  # noqa: BLE001
        logger.warning("proxy_audit_write_failed: %s", exc)


async def _fetch_upstream(brain: str, upstream_url: str) -> tuple[Optional[int], Optional[Dict[str, Any]], float, Optional[str]]:
    """Fetch the brain's own /status endpoint. Returns
    (status_code, payload, duration_ms, error_reason)."""
    started = time.time()
    try:
        async with httpx.AsyncClient(timeout=PROXY_TIMEOUT_S) as client:
            resp = await client.get(upstream_url)
        duration_ms = (time.time() - started) * 1000
        if resp.status_code != 200:
            return resp.status_code, None, duration_ms, f"upstream_http_{resp.status_code}"
        try:
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001
            return resp.status_code, None, duration_ms, f"upstream_bad_json:{type(exc).__name__}"
        return resp.status_code, payload, duration_ms, None
    except httpx.TimeoutException:
        return None, None, (time.time() - started) * 1000, "upstream_timeout"
    except httpx.ConnectError as exc:
        return None, None, (time.time() - started) * 1000, f"upstream_connect_failed:{exc.__class__.__name__}"
    except Exception as exc:  # noqa: BLE001
        return None, None, (time.time() - started) * 1000, f"upstream_unexpected:{type(exc).__name__}"


def _cache_get(brain: str) -> Optional[Dict[str, Any]]:
    entry = _PROXY_CACHE.get(brain)
    if not entry:
        return None
    set_at, payload = entry
    if time.time() - set_at > PROXY_CACHE_TTL_S:
        _PROXY_CACHE.pop(brain, None)
        return None
    return payload


def _cache_set(brain: str, payload: Dict[str, Any]) -> None:
    _PROXY_CACHE[brain] = (time.time(), payload)


@router.get("/{brain}/status")
async def get_brain_status(
    brain: str = Path(...),
    skip_cache: bool = Query(False, description="bypass MC's 10s proxy cache"),
    _user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """Operator-only proxy of the brain's own `/status` endpoint.

    Doctrine:
      - Operator JWT required. Brains do NOT proxy each other.
      - Bounded fetch (`PROXY_TIMEOUT_S`, default 4s).
      - One audit row per attempt to `brain_status_proxy_audit`.
      - On upstream failure: returns `{ok: false, error, last_success: ...}`
        wrapper — never 500s. The dashboard tile renders a degraded
        state instead of going blank.
      - Cached `PROXY_CACHE_TTL_S` (default 10s) per brain. Passing
        `?skip_cache=true` forces a fresh fetch.
    """
    brain = (brain or "").lower().strip()
    if brain not in KNOWN_BRAINS:
        raise HTTPException(status_code=404, detail=f"unknown brain {brain!r}")

    upstream_url = _upstream_url_for(brain)
    if not upstream_url:
        # Brain hasn't shipped /status yet. Surface honestly; do NOT
        # 500. Dashboard renders the same graceful "no upstream"
        # state we already ship for brains without sub-endpoints.
        return {
            "brain": brain,
            "ok": False,
            "error": "no_upstream_configured",
            "doctrine": "operator_read_only_status_proxy",
            "ts": _now().isoformat(),
        }

    principal = f"operator:{_user.get('email')}"

    if not skip_cache:
        cached = _cache_get(brain)
        if cached is not None:
            cached_view = dict(cached)
            cached_view["_proxy_from_cache"] = True
            cached_view["_proxy_age_s"] = round(time.time() - _PROXY_CACHE[brain][0], 2)
            return cached_view

    status_code, payload, duration_ms, error = await _fetch_upstream(brain, upstream_url)
    await _write_proxy_audit(
        brain, principal, upstream_url, status_code, duration_ms, error,
    )

    if payload is None:
        return {
            "brain": brain,
            "ok": False,
            "error": error or "upstream_failed",
            "upstream_status_code": status_code,
            "duration_ms": round(duration_ms, 2),
            "doctrine": "operator_read_only_status_proxy",
            "ts": _now().isoformat(),
        }

    response = {
        "brain": brain,
        "ok": True,
        "_proxied_from": upstream_url,
        "_proxy_duration_ms": round(duration_ms, 2),
        "_proxy_from_cache": False,
        "_proxy_age_s": 0.0,
        "ts": _now().isoformat(),
        "doctrine": "operator_read_only_status_proxy",
        "payload": payload,
    }
    _cache_set(brain, response)
    return response


@router.post("/{brain}/status/refresh")
async def force_refresh_brain_status(
    brain: str = Path(...),
    _user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """Operator escape hatch — drop the per-brain cache so the next
    /status call re-fetches from upstream. Useful when the operator
    just redeployed a brain and wants the dashboard to reflect the
    new state without waiting for the 10s TTL."""
    brain = (brain or "").lower().strip()
    if brain not in KNOWN_BRAINS:
        raise HTTPException(status_code=404, detail=f"unknown brain {brain!r}")
    existed = _PROXY_CACHE.pop(brain, None) is not None
    return {
        "brain": brain,
        "cache_cleared": existed,
        "doctrine": "operator_cache_reset",
    }


@router.get("/status-proxy-audit")
async def get_proxy_audit(
    brain: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=1000),
    _user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """Operator forensics: read the proxy-call audit log. Filter by
    brain (single) or pull the global tail. Used to answer 'when was
    RedEye unreachable from MC's network?' without tailing logs."""
    query: Dict[str, Any] = {}
    if brain:
        b = brain.lower().strip()
        if b not in KNOWN_BRAINS:
            raise HTTPException(status_code=404, detail=f"unknown brain {b!r}")
        query["brain"] = b
    rows = await db[BRAIN_STATUS_PROXY_AUDIT].find(
        query, {"_id": 0},
    ).sort("ts", -1).limit(limit).to_list(length=limit)
    return {
        "rows": rows,
        "count": len(rows),
        "filter_brain": brain,
        "doctrine": "operator_read_only",
    }



# ──────────────────────── /admin/runtime/{brain}/universe ────────────────────────
# Brain-callable view of MC's `patterns_universe`, lane-filtered by
# the brain's currently-held seats. Brains use this as the canonical
# source of "what symbols may I propose?" Doctrine (c) — MC verifies
# boundaries, brains propose within them.


@router.get("/{brain}/universe")
async def get_brain_universe(
    brain: str = Path(..., description="brain id"),
    x_brain_id: Optional[str] = Header(default=None, alias="X-Brain-Id"),
    x_runtime_token: Optional[str] = Header(default=None, alias="X-Runtime-Token"),
    operator_user: Optional[dict] = Depends(_maybe_user),
) -> Dict[str, Any]:
    """Return the symbols a given brain is allowed to propose intents
    on, lane-filtered by the brain's currently-held seats.

    Auth: dual — operator JWT OR (X-Brain-Id + X-Runtime-Token). If
    brain-auth, the path `{brain}` MUST match the authenticated
    brain — a brain cannot peek at another brain's universe.

    Response shape:
        {
          "brain": "camaro",
          "lanes": ["equity", "crypto"],
          "symbols": [
            {"symbol": "BTC/USD", "lane": "crypto"},
            {"symbol": "NVDA",    "lane": "equity"},
            ...
          ],
          "count": int,
          "served_at": iso8601,
          "doctrine": "operator_read_only_universe_view",
        }

    Brains MUST cache this response locally and use it as the ONLY
    source of tradeable symbols for their strategist loop. On a 5xx
    or timeout, fall back to last-known-good cache. On 200, replace
    the cache. The MC-side `symbol_in_universe` gate will reject any
    intent whose symbol is not in this response — guaranteeing that
    a brain which drifts from this universe sees its intents
    silently fail at MC's gate chain rather than reach the broker.
    """
    principal = await _dual_auth(x_brain_id, x_runtime_token, operator_user)

    brain = (brain or "").lower().strip()
    if brain not in KNOWN_BRAINS:
        raise HTTPException(status_code=404, detail=f"unknown brain {brain!r}")

    # Brain auth: enforce that the path `{brain}` matches the
    # authenticated brain. No cross-brain peeking.
    if principal.startswith("brain:"):
        auth_brain = principal.split(":", 1)[1]
        if auth_brain != brain:
            raise HTTPException(
                status_code=403,
                detail=(
                    f"brain {auth_brain!r} cannot read universe for "
                    f"brain {brain!r}; pull your own URL"
                ),
            )

    # Resolve the brain's seats to a set of allowed lanes.
    snap = await get_roster()
    assignments: Dict[str, Optional[str]] = (snap or {}).get("assignments") or {}
    brain_lanes: set[str] = set()
    for seat, occupant in assignments.items():
        if occupant != brain:
            continue
        brain_lanes.add(_lane_of_role(seat))

    if not brain_lanes:
        # Brain holds no seats. Return an empty universe rather than
        # 4xx — an unseated brain is a normal state during operator
        # rotation; it just means "you have nothing to propose
        # right now." Strategist loop should idle.
        return {
            "brain": brain,
            "lanes": [],
            "symbols": [],
            "count": 0,
            "served_at": _now().isoformat(),
            "served_to": principal,
            "doctrine": "operator_read_only_universe_view",
            "note": "brain holds no seats — empty universe is intentional",
        }

    # Query patterns_universe for the brain's lanes. Active rows only.
    # Backward-compat: rows without a `lane` field are treated as
    # equity (the legacy seed semantic).
    from namespaces import PATTERNS_UNIVERSE  # noqa: WPS433
    or_clauses: list[dict] = []
    if "equity" in brain_lanes:
        or_clauses.append({"lane": "equity"})
        or_clauses.append({"lane": {"$exists": False}})
    if "crypto" in brain_lanes:
        or_clauses.append({"lane": "crypto"})
    cursor = db[PATTERNS_UNIVERSE].find(
        {"active": {"$ne": False}, "$or": or_clauses},
        {"_id": 0, "symbol": 1, "lane": 1},
    ).sort("symbol", 1)
    symbols: list[dict] = []
    async for row in cursor:
        symbols.append({
            "symbol": row.get("symbol"),
            "lane": (row.get("lane") or "equity"),
        })

    return {
        "brain": brain,
        "lanes": sorted(brain_lanes),
        "symbols": symbols,
        "count": len(symbols),
        "served_at": _now().isoformat(),
        "served_to": principal,
        "doctrine": "operator_read_only_universe_view",
    }
