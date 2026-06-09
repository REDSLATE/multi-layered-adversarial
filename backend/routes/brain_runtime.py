"""Brain-callable runtime endpoints (rewritten 2026-02-XX).

The 4 permanent neutral brains (Camino / Barracuda / Hellcat / GTO)
run IN-PROCESS inside MC's FastAPI event loop. There is no external
sidecar to proxy to. The status surface synthesizes state directly
from MC's own collections (`shared_heartbeats`, `sovereign_state`,
`shared_intents`) plus the live in-process runner stats.

The previous external-sidecar proxy infrastructure
(`_fetch_upstream`, `_PROXY_CACHE`, `brain_status_proxy_audit`,
`{BRAIN}_STATUS_URL` env vars, `/status/refresh`,
`/status-proxy-audit`) was REMOVED — it timed out more than it
succeeded and made the dashboard look like every brain was
disconnected. If external sidecars ever come back, restore from git
history; do NOT bolt a "future-proof" proxy onto this file.

Operator-facing endpoints:

  GET  /api/admin/runtime/roster?caller={brain}
        — Lean seat roster, lane-resolved per role. Dual auth
          (operator JWT OR runtime-token).

  GET  /api/admin/runtime/{brain}/status
        — Composite in-process status (identity / seats / heartbeat /
          intents / runner stats). Operator JWT.

  GET  /api/admin/runtime/{brain}/universe
        — The symbols the brain may propose, lane-filtered by its
          held seats. Dual auth; brain auth pinned to the path brain.

All endpoints are READ-ONLY.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Path

from auth import get_current_user
from db import db
from namespaces import (
    LIVE_RUNTIMES,
    SHARED_HEARTBEATS,
    SHARED_INTENTS,
    SOVEREIGN_STATE,
)
from shared.roster import CRYPTO_LANE_ROLES, get_roster


logger = logging.getLogger("risedual.brain_runtime")
router = APIRouter(prefix="/admin/runtime", tags=["brain-runtime"])


KNOWN_BRAINS: tuple[str, ...] = tuple(LIVE_RUNTIMES)


# ──────────────────────── In-process runner accessor ────────────────────────
# Lazy imports: `external.brains.runner` lives outside /app/backend.
# `server.py` adds /app to sys.path during lifespan startup, so a
# module-level import would fail silently at boot. Importing per-call
# keeps the request handler robust regardless of process state.

def _local_runner_for(brain: str):
    try:
        import sys as _sys
        if "/app" not in _sys.path:
            _sys.path.insert(0, "/app")
        from external.brains.runner import runner_for  # type: ignore
        return runner_for(brain)
    except Exception:  # noqa: BLE001
        return None


# ──────────────────────── helpers ────────────────────────

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _expected_token_for(brain: str) -> str:
    return os.environ.get(f"{brain.upper()}_INGEST_TOKEN", "")


def _lane_of_role(role: str) -> str:
    return "crypto" if role in CRYPTO_LANE_ROLES else "equity"


def _age_seconds(iso: Optional[str], now: datetime) -> Optional[float]:
    if not iso:
        return None
    try:
        t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return (now - t).total_seconds()
    except (ValueError, AttributeError):
        return None


# ──────────────────────── Dual auth (operator OR brain token) ────────────────────────

async def _dual_auth(
    x_brain_id: Optional[str],
    x_runtime_token: Optional[str],
    operator_user: Optional[dict],
) -> str:
    """Returns the auth principal for audit trails. Either operator JWT
    or a (brain-id, runtime-token) pair MUST validate, else 401."""
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

@router.get("/roster")
async def get_brain_roster(
    caller: Optional[str] = None,
    x_brain_id: Optional[str] = Header(default=None, alias="X-Brain-Id"),
    x_runtime_token: Optional[str] = Header(default=None, alias="X-Runtime-Token"),
    operator_user: Optional[dict] = Depends(_maybe_user),
) -> Dict[str, Any]:
    """Brain-callable roster — lean payload with seat assignments and
    a precomputed `your_seats` list when `caller` is set.

    Doctrine: read-only seat view. Auth: dual.
    """
    principal = await _dual_auth(x_brain_id, x_runtime_token, operator_user)

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


# ──────────────────────── /admin/runtime/{brain}/status ────────────────────────

async def _build_in_process_status(brain: str) -> Dict[str, Any]:
    """Compose a status payload from MC's own state for an in-process
    brain. Section names match what the dashboard's
    BrainProxiedStatusTile already renders (`identity`, `seats`,
    `heartbeat`, `intents`) so no frontend change is needed.
    """
    runner = _local_runner_for(brain)
    runner_stats = runner.stats if runner else None
    now = _now()

    hb_doc = await db[SHARED_HEARTBEATS].find_one(
        {"runtime": brain},
        {"last_seen": 1, "status": 1, "heartbeat_count": 1},
    )
    hb_iso = (hb_doc or {}).get("last_seen")
    hb_age = _age_seconds(hb_iso, now)

    sv_doc = await db[SOVEREIGN_STATE].find_one(
        {"brain": brain},
        {"updated_at": 1, "mode": 1, "live_trading_enabled": 1, "notes": 1},
    )
    sv_iso = (sv_doc or {}).get("updated_at")
    sv_age = _age_seconds(sv_iso, now)

    # Intent windows + per-action breakdown over 24h. Filter by
    # `stack` (the brain that emitted) — same field the
    # sidecar_diagnostics aggregator uses so the surfaces stay
    # consistent.
    cutoff_24h = (now - timedelta(hours=24)).isoformat()
    cutoff_1h = (now - timedelta(hours=1)).isoformat()
    count_24h = await db[SHARED_INTENTS].count_documents({
        "stack": brain, "ingest_ts": {"$gte": cutoff_24h},
    })
    count_1h = await db[SHARED_INTENTS].count_documents({
        "stack": brain, "ingest_ts": {"$gte": cutoff_1h},
    })
    by_action_cursor = db[SHARED_INTENTS].aggregate([
        {"$match": {"stack": brain, "ingest_ts": {"$gte": cutoff_24h}}},
        {"$group": {"_id": "$action", "count": {"$sum": 1}}},
    ])
    by_action: Dict[str, int] = {}
    async for row in by_action_cursor:
        by_action[str(row.get("_id") or "UNK").upper()] = int(row.get("count", 0))
    total_intents = await db[SHARED_INTENTS].count_documents({"stack": brain})

    # Seats lane-resolved from the live roster snapshot.
    snap = await get_roster()
    assignments: Dict[str, Optional[str]] = (snap or {}).get("assignments") or {}
    seats_held = [
        {"seat": seat, "lane": _lane_of_role(seat)}
        for seat, occupant in assignments.items()
        if occupant == brain
    ]

    display_name = runner_stats.get("display_name") if runner_stats else brain.title()
    identity = {
        "app_name": "risedual-mc",
        "env_name": os.environ.get("ENV_NAME") or os.environ.get("ENVIRONMENT") or "preview",
        "git_sha": os.environ.get("GIT_SHA") or os.environ.get("RAILWAY_GIT_COMMIT_SHA") or "in-process",
        "broker_mode": "kraken+public",
        "sidecar_version": f"in-process/{display_name}",
        # All connectivity flags are TRUE by definition for in-process —
        # no cross-network handshake to fail.
        "mc_url_set": True,
        "ingest_token_set": True,
        "mc_base_url_set": True,
        "heartbeat_token_set": True,
        "checkin_worker_eligible": True,
    }

    return {
        "identity": identity,
        "seats": {
            "count": len(seats_held),
            "seats_held": seats_held,
        },
        "heartbeat": {
            "enabled": True,
            "alive": hb_age is not None and hb_age < 300,
            "tick_s": runner_stats.get("tick_count") if runner_stats else None,
            "last_source": "in-process loopback",
            "last_opinion_id": None,
            "seconds_since_last_opinion": round(hb_age, 1) if hb_age is not None else None,
            "last_tick_ok": True,
            "last_tick_error": None,
            "last_seen": hb_iso,
            "sovereign_age_s": round(sv_age, 1) if sv_age is not None else None,
            "sovereign_mode": (sv_doc or {}).get("mode"),
            "sovereign_live_trading": (sv_doc or {}).get("live_trading_enabled"),
        },
        "intents": {
            "total": total_intents,
            "last_1h": count_1h,
            "last_24h": count_24h,
            "by_action": by_action,
        },
        "in_process_runner": runner_stats,
    }


@router.get("/{brain}/status")
async def get_brain_status(
    brain: str = Path(...),
    _user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """Operator-only composite status for the named in-process brain.

    Returns the same wrapper shape (`{brain, ok, _proxied_from,
    payload}`) the dashboard's BrainProxiedStatusTile already renders,
    so frontend code is unchanged.
    """
    brain = (brain or "").lower().strip()
    if brain not in KNOWN_BRAINS:
        raise HTTPException(status_code=404, detail=f"unknown brain {brain!r}")

    try:
        in_proc = await _build_in_process_status(brain)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "in_process_status_build_failed brain=%s err=%s", brain, exc,
        )
        # Surface as `ok=false` so the dashboard renders a degraded
        # state instead of going blank. NOT a 500 — this endpoint is
        # consumed by an auto-polling tile and must stay resilient.
        return {
            "brain": brain,
            "ok": False,
            "error": "in_process_build_failed",
            "doctrine": "in_process_runtime_status",
            "ts": _now().isoformat(),
        }

    return {
        "brain": brain,
        "ok": True,
        "_proxied_from": "in_process",
        "_proxy_duration_ms": 0.0,
        "_proxy_from_cache": False,
        "_proxy_age_s": 0.0,
        "ts": _now().isoformat(),
        "doctrine": "in_process_runtime_status",
        "payload": in_proc,
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
    brain-auth, the path `{brain}` MUST match the authenticated brain.

    Brains MUST cache locally and use this as the ONLY source of
    tradeable symbols. The MC-side `symbol_in_universe` gate will
    reject any intent whose symbol is not in this response.
    """
    principal = await _dual_auth(x_brain_id, x_runtime_token, operator_user)

    brain = (brain or "").lower().strip()
    if brain not in KNOWN_BRAINS:
        raise HTTPException(status_code=404, detail=f"unknown brain {brain!r}")

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

    snap = await get_roster()
    assignments: Dict[str, Optional[str]] = (snap or {}).get("assignments") or {}
    brain_lanes: set[str] = set()
    for seat, occupant in assignments.items():
        if occupant != brain:
            continue
        brain_lanes.add(_lane_of_role(seat))

    if not brain_lanes:
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
