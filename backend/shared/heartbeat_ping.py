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
from namespaces import DISCUSSION_PARTICIPANTS, SHARED_HEARTBEATS, SOVEREIGN_STATE


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


@router.get("/heartbeat-status/{brain}")
async def heartbeat_status(brain: str):
    """Read-only status for the operator dashboard. No auth required —
    leaks only that the brain has/hasn't pinged recently, which the
    public /ping pages already expose.

    Combines TWO signals so legacy ingest traffic doesn't false-green
    the indicator:

      * `heartbeat`: shared_heartbeats row (any heartbeat source)
      * `contribution`: sovereign_state row (the proof that a real
        sovereign sidecar is running and posting its weights)

    Combined verdict:
      * `connected`  — heartbeat <90s AND contribution <300s
                       (real sidecar is alive AND posting state)
      * `partial`    — heartbeat <90s but contribution missing or
                       stale (legacy ingest only, or sidecar crashed
                       between contributions)
      * `stale`      — contribution last seen 5-30 min ago
      * `dead`       — neither signal recent
      * `never`      — neither signal has EVER been seen for this brain

    The frontend LivePulse renders `connected` as the pulsing green
    state. `partial` is amber and surfaces the most common confusion
    mode ("legacy heartbeats / no real sidecar").
    """
    brain = brain.lower()
    if brain not in DISCUSSION_PARTICIPANTS:
        raise HTTPException(status_code=404, detail=f"unknown brain {brain!r}")

    now = datetime.now(timezone.utc)

    def _age(iso: str | None) -> float | None:
        if not iso:
            return None
        try:
            t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
            return (now - t).total_seconds()
        except (ValueError, AttributeError):
            return None

    hb = await db[SHARED_HEARTBEATS].find_one({"runtime": brain}, {"_id": 0})
    hb_iso = (hb or {}).get("last_seen")
    hb_age = _age(hb_iso)
    hb_first_seen = (hb or {}).get("first_seen_at")
    hb_count = (hb or {}).get("heartbeat_count")

    sv = await db[SOVEREIGN_STATE].find_one({"brain": brain}, {"_id": 0})
    sv_iso = (sv or {}).get("updated_at")
    sv_age = _age(sv_iso)
    sv_first_seen = (sv or {}).get("first_seen_at")
    sv_count = (sv or {}).get("contribution_count")

    hb_fresh = hb_age is not None and hb_age < 90
    sv_fresh = sv_age is not None and sv_age < 300       # ≤ 5 min
    sv_stale_band = sv_age is not None and sv_age < 1800  # ≤ 30 min

    if hb_iso is None and sv_iso is None:
        connected = "never"
    elif hb_fresh and sv_fresh:
        connected = "connected"
    elif hb_fresh and not sv_fresh:
        # Heartbeat looks alive but no real sovereign contribution —
        # either legacy ingest traffic only, or sidecar crashed
        # between ticks.
        connected = "partial"
    elif sv_stale_band:
        connected = "stale"
    else:
        connected = "dead"

    # Pick the more recent of the two signals as the "last_seen" that
    # the operator UI surfaces (matches whichever side of the wire is
    # most alive).
    ages = [(a, iso) for a, iso in [(hb_age, hb_iso), (sv_age, sv_iso)] if a is not None]
    last_seen_iso = None
    last_seen_age = None
    if ages:
        ages.sort(key=lambda x: x[0])
        last_seen_age = round(ages[0][0], 1)
        last_seen_iso = ages[0][1]

    # Uptime: time since the brain first contacted MC, using whichever
    # of the two signals saw it earliest (heartbeat or contribution).
    first_seen_candidates = [
        x for x in (hb_first_seen, sv_first_seen) if x
    ]
    uptime_seconds: float | None = None
    first_seen_iso: str | None = None
    if first_seen_candidates:
        first_seen_iso = min(first_seen_candidates)
        uptime_seconds = round(_age(first_seen_iso) or 0.0, 1)

    return {
        "runtime": brain,
        "connected": connected,
        "last_seen": last_seen_iso,
        "age_seconds": last_seen_age,
        "first_seen_at": first_seen_iso,
        "uptime_seconds": uptime_seconds,
        # Diagnostic detail so the operator can see WHY a brain is
        # marked partial / stale (e.g., "heartbeat 4s, contribution
        # never").
        "heartbeat_age_seconds": (
            round(hb_age, 1) if hb_age is not None else None
        ),
        "heartbeat_count": hb_count,
        "contribution_age_seconds": (
            round(sv_age, 1) if sv_age is not None else None
        ),
        "contribution_count": sv_count,
    }
