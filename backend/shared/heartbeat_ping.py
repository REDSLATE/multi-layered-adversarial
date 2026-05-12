"""Public heartbeat-ping endpoint.

Lets you keep a brain's heartbeat row fresh without running a sidecar
process anywhere — point any HTTP client (browser bookmark, UptimeRobot,
curl on a cron, BetterUptime, healthchecks.io) at:

    GET/POST /api/heartbeat-ping/{brain}?token=<RUNTIME_INGEST_TOKEN>

Every successful hit upserts shared_heartbeats with detail.source =
"heartbeat_ping" + the caller's User-Agent so the dashboard can show
who's keeping the brain alive.

Doctrine note:
    A heartbeat-ping is a real network beat — it proves something
    outside Mission Control is regularly calling. That's a stronger
    liveness signal than the in-MC proxy beater we considered. It is
    still *weaker* than a true sidecar that knows the runtime's
    internal state — keep this as a stopgap until each brain has a
    proper agent.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Header, Query, Request
from pydantic import BaseModel

from db import db
from namespaces import DISCUSSION_PARTICIPANTS, SHARED_HEARTBEATS


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _expected_token(brain: str) -> str:
    """Resolve the per-brain ingest token from .env. Falls back to empty
    string (every call will then fail) so misconfiguration is loud."""
    return os.environ.get(f"{brain.upper()}_INGEST_TOKEN", "") or ""


router = APIRouter(tags=["heartbeat-ping"])


class PingOut(BaseModel):
    ok: bool
    runtime: str
    last_seen: str
    source: str
    note: str


async def _do_ping(brain: str, token: str, user_agent: str) -> PingOut:
    if brain not in DISCUSSION_PARTICIPANTS:
        raise HTTPException(
            status_code=404,
            detail=f"unknown brain {brain!r}",
        )
    expected = _expected_token(brain)
    if not expected:
        raise HTTPException(
            status_code=500,
            detail=f"no ingest token configured for {brain}; "
                   f"set {brain.upper()}_INGEST_TOKEN in backend/.env",
        )
    if token != expected:
        raise HTTPException(status_code=401, detail="invalid token")

    now = _now_iso()
    await db[SHARED_HEARTBEATS].update_one(
        {"runtime": brain},
        {"$set": {
            "runtime": brain,
            "status": "ok",
            "last_seen": now,
            "detail": {
                "source": "heartbeat_ping",
                "via": "public ping endpoint",
                "user_agent": user_agent[:200],
            },
        }},
        upsert=True,
    )
    return PingOut(
        ok=True,
        runtime=brain,
        last_seen=now,
        source="heartbeat_ping",
        note=(
            f"{brain} heartbeat refreshed. Set up an uptime monitor "
            f"(UptimeRobot, BetterUptime, etc.) to hit this URL every "
            f"1-5 min to keep the row green permanently."
        ),
    )


@router.api_route(
    "/heartbeat-ping/{brain}",
    methods=["GET", "POST", "HEAD"],
    response_model=PingOut,
)
async def heartbeat_ping(
    brain: str,
    request: Request,
    token: str = Query(default="", description="brain ingest token"),
    x_runtime_token: str | None = Header(default=None, alias="X-Runtime-Token"),
):
    """Public — no JWT. Authn via the per-brain ingest token, accepted
    either as `?token=` (so a browser bookmark works) or as
    `X-Runtime-Token` (so a real client can be header-driven)."""
    effective_token = token or x_runtime_token or ""
    ua = request.headers.get("user-agent", "")
    return await _do_ping(brain.lower(), effective_token, ua)
