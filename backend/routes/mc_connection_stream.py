"""Server-Sent Events stream — live frontend updates.

Doctrine pin (2026-06-10, P2):
The dashboard was strictly poll-based. Operators waited 10-30s
between refreshes to see new intents land, new broker fills come
in, or new position misreads. This endpoint pushes those events
the instant they're persisted — sub-second latency, no thundering
herd on every page load.

The stream multiplexes 4 event types:

    event: heartbeat
    data: {"ts": "2026-06-10T10:30:00Z"}

    event: intent
    data: {"intent_id": "...", "stack": "barracuda", "action": "BUY", ...}

    event: broker_fill
    data: {"symbol": "AAPL", "side": "BUY", "qty": ..., ...}

    event: position_misread
    data: {"symbol": "AAPL", "brain": "barracuda", "assumed_side": "flat", ...}

    event: regime
    data: {"regime": "chop", "ts": "..."}

Implementation:
    * Polling-based (NOT change-streams) — Mongo standalone replica
      sets aren't guaranteed in every deploy; polling is portable.
    * Single shared poll cursor per connection — server hands the
      client every NEW row since they connected, then increments
      its "since" pointer.
    * Heartbeat every 15s so proxies don't reap the connection.
    * Auth: token can be passed as `?token=` query param because
      `EventSource` in browsers can't set custom Authorization
      headers. Tokens flow through the existing JWT verifier — no
      new auth surface.
    * Concurrency-safe: every connection gets its own coroutine
      with its own cursor; no shared state between connections.

Frontend usage:
    const evt = new EventSource(`/api/mc-connection/stream?token=${TOKEN}`);
    evt.addEventListener("intent", (e) => { ... });
    evt.addEventListener("position_misread", (e) => { ... });
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import jwt
from fastapi import APIRouter, HTTPException, Query, Request
from sse_starlette.sse import EventSourceResponse

from db import db
from namespaces import SHARED_INTENTS

logger = logging.getLogger("risedual.mc_connection_stream")


router = APIRouter(prefix="/mc-connection", tags=["mc-connection"])


# Poll interval. 2s is the sweet spot — fast enough to feel live,
# slow enough that even a busy MC (4 brains × universe-sized scans)
# doesn't drown the Mongo with reads.
_POLL_INTERVAL_SEC = float(os.environ.get("MC_STREAM_POLL_SEC", "2.0"))

# Heartbeat every 15s. Proxies (Cloudflare, nginx) typically reap
# idle connections at 30-60s; 15s gives us safety margin.
_HEARTBEAT_SEC = float(os.environ.get("MC_STREAM_HEARTBEAT_SEC", "15.0"))

# Per-poll row cap — protect against catching up after a long pause
# (e.g., client backgrounded a tab for an hour). Older events drop
# off the stream; the dashboard can always REST-fetch the backlog.
_MAX_ROWS_PER_POLL = 100


def _verify_token(token: Optional[str]) -> dict:
    """Verify a JWT token from the query string. Mirrors `auth.get_current_user`
    but accepts the token as an explicit arg (so we can read it from
    `?token=` since browser EventSource can't set headers)."""
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    secret = os.environ.get("JWT_SECRET")
    if not secret:
        # Backend should always have JWT_SECRET; failing here means
        # misconfig — better to 500 loud than 401 quiet.
        raise HTTPException(status_code=500, detail="JWT secret not configured")
    try:
        payload = jwt.decode(token, secret, algorithms=["HS256"])
        if payload.get("type") != "access":
            raise HTTPException(status_code=401, detail="Invalid token type")
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")
    return payload


def _sse(event: str, data: dict) -> dict:
    """sse_starlette expects dicts with `event`/`data` keys — formats
    them into the wire spec automatically."""
    return {"event": event, "data": json.dumps(data, default=str)}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _event_stream(initial_ts: str):
    """Async generator yielding SSE events. Drives the poll loop."""
    # Per-collection "high watermark" cursors. Start at `initial_ts`
    # so the client only sees events that happen AFTER they connected.
    intents_since = initial_ts
    fills_since = initial_ts
    misreads_since = initial_ts
    last_heartbeat_at = asyncio.get_event_loop().time()
    # Track the last regime emitted so we don't spam the wire on
    # every poll — only push when it CHANGES.
    last_regime: Optional[str] = None

    # Open marker so the client knows the stream is alive.
    yield _sse("hello", {"ts": initial_ts, "poll_interval_sec": _POLL_INTERVAL_SEC})

    while True:
        try:
            # ── Intents ─────────────────────────────────────────
            cur = (
                db[SHARED_INTENTS]
                .find(
                    {"ingest_ts": {"$gt": intents_since}},
                    {
                        "_id": 0,
                        "intent_id": 1, "stack": 1, "action": 1,
                        "symbol": 1, "lane": 1, "confidence": 1,
                        "gate_state": 1, "ingest_ts": 1,
                        "snapshot": 1,
                    },
                )
                .sort("ingest_ts", 1)
                .limit(_MAX_ROWS_PER_POLL)
            )
            current_regime: Optional[str] = None
            async for row in cur:
                intents_since = max(intents_since, row.get("ingest_ts") or intents_since)
                # Extract regime from the snapshot if present; drop the
                # full snapshot from the wire payload (too noisy).
                snap = row.pop("snapshot", None) or {}
                if isinstance(snap, dict):
                    regime = snap.get("market_regime")
                    if regime:
                        current_regime = regime
                yield _sse("intent", row)

            # Emit a regime event only when the regime changed during
            # this poll batch.
            if current_regime and current_regime != last_regime:
                last_regime = current_regime
                yield _sse("regime", {
                    "regime": current_regime,
                    "ts": _now_iso(),
                })

            # ── Broker fills ────────────────────────────────────
            cur = (
                db["shared_broker_fills"]
                .find(
                    {"ingested_at": {"$gt": fills_since}},
                    {
                        "_id": 0,
                        "symbol": 1, "side": 1, "qty": 1, "price": 1,
                        "net_amount": 1, "broker": 1, "timestamp": 1,
                        "ingested_at": 1,
                    },
                )
                .sort("ingested_at", 1)
                .limit(_MAX_ROWS_PER_POLL)
            )
            async for row in cur:
                fills_since = max(fills_since, row.get("ingested_at") or fills_since)
                yield _sse("broker_fill", row)

            # ── Position misreads ───────────────────────────────
            cur = (
                db["shared_position_misreads"]
                .find(
                    {"detected_at": {"$gt": misreads_since}},
                    {"_id": 0},
                )
                .sort("detected_at", 1)
                .limit(_MAX_ROWS_PER_POLL)
            )
            async for row in cur:
                misreads_since = max(
                    misreads_since, row.get("detected_at") or misreads_since,
                )
                yield _sse("position_misread", row)

            # ── Heartbeat ───────────────────────────────────────
            loop_now = asyncio.get_event_loop().time()
            if (loop_now - last_heartbeat_at) >= _HEARTBEAT_SEC:
                yield _sse("heartbeat", {"ts": _now_iso()})
                last_heartbeat_at = loop_now

        except asyncio.CancelledError:
            # Client disconnected — clean exit. Re-raise so the
            # ASGI server can finish tearing down the response.
            raise
        except Exception as exc:  # noqa: BLE001
            # Don't kill the stream on a transient Mongo blip — log
            # and keep polling. Send a typed error event so the
            # client can decide whether to reconnect.
            logger.warning("mc_stream poll error: %s: %s", type(exc).__name__, exc)
            yield _sse("error", {
                "kind": type(exc).__name__,
                "detail": str(exc)[:200],
            })

        await asyncio.sleep(_POLL_INTERVAL_SEC)


@router.get("/stream")
async def mc_connection_stream(
    request: Request,
    token: Optional[str] = Query(None, description="JWT access token"),
):
    """Long-lived SSE stream of MC runtime events.

    Auth: token via `?token=` query string (EventSource limitation).
    Falls back to cookie/Authorization header for non-browser clients
    (curl, httpx, etc).
    """
    # Prefer the explicit query param (browser EventSource path);
    # fall back to cookie / header for server-side or curl clients.
    if not token:
        token = request.cookies.get("access_token")
    if not token:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
    _verify_token(token)

    initial_ts = _now_iso()
    return EventSourceResponse(
        _event_stream(initial_ts),
        ping=int(_HEARTBEAT_SEC),  # sse_starlette also sends comment-pings
    )
