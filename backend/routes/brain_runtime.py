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

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Path

from auth import get_current_user
from db import db
from namespaces import (
    LIVE_RUNTIMES,
    SHARED_HEARTBEATS,
    SOVEREIGN_STATE,
)
from shared.roster import CRYPTO_LANE_ROLES, get_roster


logger = logging.getLogger("risedual.brain_runtime")
router = APIRouter(prefix="/admin/runtime", tags=["brain-runtime"])


KNOWN_BRAINS: tuple[str, ...] = tuple(LIVE_RUNTIMES)


# ──────────────────────── In-process runner accessor ────────────────────────
# 2026-07-12 (P3 step 3): the in-process brain runners in
# `external/brains/runner.py` were deleted after the pulse
# migration. This accessor now always returns None — any callers
# that still hit it get the "runner not present" branch, which
# was the safe fail-soft behavior before.


def _local_runner_for(brain: str):
    _ = brain  # kept to preserve the callable signature
    return None


# ──────────────────────── helpers ────────────────────────

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _expected_token_for(brain: str) -> str:
    from shared.brain_token import expected_ingest_token
    return expected_ingest_token(brain)


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


# ══════════════════════════════════════════════════════════════════
#  Stack /status decoration — 3-clock write_health (2026-02-20)
# ══════════════════════════════════════════════════════════════════
#
# `_equity_market_open` is a coarse approximation of US regular
# equity hours (Mon–Fri, 09:30–16:00 ET, no holiday calendar). The
# only consumer is `_write_health_band` — a wrong answer here at
# worst relaxes the DEAD threshold by an hour, it can't hide a real
# failure. A precise session model belongs in a shared calendar
# module, not this status decorator.

_HEALTHY_MAX_AGE_S = 15 * 60
_STALE_MAX_AGE_S = 60 * 60
_HEARTBEAT_ALIVE_MAX_AGE_S = 5 * 60
_CLOSED_SESSION_DEAD_AGE_S = 12 * 60 * 60  # override during off-hours


def _equity_market_open(now: datetime) -> bool:
    """True during US equity regular session (M-F, 09:30-16:00 ET)."""
    try:
        # datetime.timezone(-5h) is EST; a proper zoneinfo lookup
        # would also handle DST but is not needed for a coarse
        # relax-threshold decision.
        from zoneinfo import ZoneInfo  # py>=3.9
        et = now.astimezone(ZoneInfo("America/New_York"))
    except Exception:  # noqa: BLE001
        et = now  # fall back to UTC; the threshold check still holds
    if et.weekday() >= 5:
        return False
    hh_mm = et.hour * 60 + et.minute
    return 9 * 60 + 30 <= hh_mm < 16 * 60


def _write_health_band(
    *,
    heartbeat_age_s: Optional[float],
    db_write_age_s: Optional[float],
    equity_open: bool,
) -> str:
    """Return one of HEALTHY / STALE / DEAD / UNKNOWN / BLIND.

    Doctrine:
      * BLIND    — heartbeat itself is stale; MC has no fresh
                   ground-truth to judge the write path.
      * UNKNOWN  — brain has never confirmed a Mongo write. Common
                   for a freshly-booted brain; not yet a failure.
      * HEALTHY  — any-intent write < 15 min old.
      * STALE    — 15 min–60 min.
      * DEAD     — >60 min AND (session is open OR write age crossed
                   the closed-session relax threshold).
    """
    if heartbeat_age_s is None or heartbeat_age_s > _HEARTBEAT_ALIVE_MAX_AGE_S:
        return "BLIND"
    if db_write_age_s is None:
        return "UNKNOWN"
    if db_write_age_s < _HEALTHY_MAX_AGE_S:
        return "HEALTHY"
    if db_write_age_s < _STALE_MAX_AGE_S:
        return "STALE"
    if not equity_open and db_write_age_s < _CLOSED_SESSION_DEAD_AGE_S:
        # Equity market closed → HOLD writes may legitimately taper.
        # Keep STALE until many hours have elapsed.
        return "STALE"
    return "DEAD"


def _decorate_brain_section(
    section: Dict[str, Any], now: datetime, equity_open: bool,
) -> Dict[str, Any]:
    """Attach `_ages` and `write_health` computed from the raw
    3-clock fields the bump helpers stamp. Non-destructive — the
    caller reads through untouched originals for anything else.
    """
    hb_ts = section.get("last_heartbeat_ts") or section.get("heartbeat_ts")
    dec_ts = section.get("last_decision_ts")
    db_ts = section.get("last_db_confirmed_intent_ts")
    dir_ts = section.get("last_db_confirmed_directional_intent_ts")
    hb_age = _age_seconds(hb_ts, now)
    dec_age = _age_seconds(dec_ts, now)
    db_age = _age_seconds(db_ts, now)
    dir_age = _age_seconds(dir_ts, now)
    band = _write_health_band(
        heartbeat_age_s=hb_age,
        db_write_age_s=db_age,
        equity_open=equity_open,
    )
    out = dict(section)
    out["_ages"] = {
        "heartbeat_age_s": round(hb_age, 1) if hb_age is not None else None,
        "decision_age_s": round(dec_age, 1) if dec_age is not None else None,
        "db_write_age_s": round(db_age, 1) if db_age is not None else None,
        "directional_write_age_s": (
            round(dir_age, 1) if dir_age is not None else None
        ),
    }
    out["write_health"] = band
    return out


async def _build_write_health_for(
    brain: str, now: datetime,
) -> Dict[str, Any]:
    """Compose the `write_health` block the per-brain status endpoint
    embeds inside `payload`. Reads the same stack doc `/stack/status`
    reads so the two endpoints tell an identical story.

    Shape (consumed by `BrainProxiedStatusTile.WriteHealthSection`):
        {
          band: HEALTHY | STALE | DEAD | UNKNOWN | BLIND,
          ages: {heartbeat_age_s, decision_age_s, db_write_age_s,
                 directional_write_age_s},
          counters: {decisions_total, intent_submit_attempts_total,
                     intent_submit_successes_total,
                     directional_submit_successes_total,
                     intent_submit_failures_total},
          last_write_receipt: {...} | null,
          last_error: {msg, ts, action, symbol} | null,
          equity_market_open: bool,
        }
    """
    from shared.brain_runtime_metrics import get_stack_status as _get_stack  # noqa: WPS433
    try:
        doc = await _get_stack()
    except Exception:  # noqa: BLE001
        doc = None
    section = ((doc or {}).get("brains") or {}).get(brain) or {}
    equity_open = _equity_market_open(now)
    decorated = _decorate_brain_section(section, now, equity_open)
    err_ts = section.get("last_intent_submit_error_ts")
    err_msg = section.get("last_intent_submit_error_msg")
    last_error = (
        {
            "msg": err_msg,
            "ts": err_ts,
            "action": section.get("last_intent_submit_error_action"),
            "symbol": section.get("last_intent_submit_error_symbol"),
        }
        if err_ts or err_msg
        else None
    )
    return {
        "band": decorated.get("write_health", "UNKNOWN"),
        "ages": decorated.get("_ages") or {},
        "counters": {
            "decisions_total": section.get("decisions_total"),
            "intent_submit_attempts_total": section.get(
                "intent_submit_attempts_total",
            ),
            "intent_submit_successes_total": section.get(
                "intent_submit_successes_total",
            ),
            "directional_submit_successes_total": section.get(
                "directional_submit_successes_total",
            ),
            "intent_submit_failures_total": section.get(
                "intent_submit_failures_total",
            ),
        },
        "last_write_receipt": section.get("last_write_receipt"),
        "last_error": last_error,
        "equity_market_open": equity_open,
    }


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

    # ── DEFENSIVE ATLAS BOUNDARY (2026-07-09 cascading-timeout hotfix) ──
    # See the doctrine comment below the count queries for full detail.
    # Defined early so heartbeat / sovereign lookups also benefit.
    async def _safe(coro, default):
        try:
            return await asyncio.wait_for(coro, timeout=3.0)
        except (asyncio.TimeoutError, Exception) as exc:  # noqa: BLE001
            logger.warning(
                "brain_runtime.status: Atlas call timed out/failed for "
                "brain=%s: %s", brain, type(exc).__name__,
            )
            return default

    atlas_partial = False

    hb_doc = await _safe(db[SHARED_HEARTBEATS].find_one(
        {"runtime": brain},
        {"last_seen": 1, "status": 1, "heartbeat_count": 1},
    ), None)
    if hb_doc is None:
        atlas_partial = True
    hb_iso = (hb_doc or {}).get("last_seen")
    hb_age = _age_seconds(hb_iso, now)

    sv_doc = await _safe(db[SOVEREIGN_STATE].find_one(
        {"brain": brain},
        {"updated_at": 1, "mode": 1, "live_trading_enabled": 1, "notes": 1},
    ), None)
    if sv_doc is None:
        atlas_partial = True
    sv_iso = (sv_doc or {}).get("updated_at")
    sv_age = _age_seconds(sv_iso, now)

    # Intent windows + per-action breakdown over 24h.
    #
    # 2026-07-09 P0 rewrite (operator directive):
    #   Reads come from the cached `brain_runtime_metrics` micro-doc
    #   populated by `shared.brain_runtime_metrics.bump_on_emit` at
    #   intent-ingest time. `refresh_windows` recomputes the rolling
    #   last_1h / last_24h / by_action off the composite index
    #   (stack_canonical, ingest_ts) with a 30-second cached TTL, so
    #   the status endpoint no longer hammers the multi-million-row
    #   `shared_intents` tape on every dashboard poll.
    #
    #   If the cached doc / refresh both fail (Atlas out or brand-
    #   new brain that has never emitted), we fall back to the
    #   runner's in-memory tick count via `runner_stats.intent_count`
    #   below and flip `atlas_partial=True` so the dashboard tile
    #   marks the section as degraded rather than blank.
    from shared.brain_legend import canonicalize_stack as _canon  # noqa: WPS433
    from shared.brain_runtime_metrics import (  # noqa: WPS433
        get_metrics, refresh_windows,
    )
    brain_c = _canon(brain) or brain

    metrics_doc: Optional[Dict[str, Any]] = await _safe(
        refresh_windows(brain_c), None,
    )
    if metrics_doc is None:
        # Refresh failed (Atlas timeout / cluster slowness). Fall back
        # to the last known cached doc, which may still be authoritative
        # for `latest_ts` / `latest_action` even if window counts are stale.
        metrics_doc = await _safe(get_metrics(brain_c), None)

    if metrics_doc is None:
        atlas_partial = True
        count_1h = None
        count_24h = None
        by_action: Dict[str, int] = {}
        latest_intent_ts = None
        latest_intent_symbol = None
        latest_intent_action = None
    else:
        count_1h = metrics_doc.get("last_1h")
        count_24h = metrics_doc.get("last_24h")
        by_action = metrics_doc.get("by_action") or {}
        latest_intent_ts = metrics_doc.get("latest_ts")
        latest_intent_symbol = metrics_doc.get("latest_symbol")
        latest_intent_action = metrics_doc.get("latest_action")
        # If windows never refreshed (bump-only path), mark degraded so
        # the operator knows the counts aren't yet authoritative.
        if count_1h is None or count_24h is None:
            atlas_partial = True

    # 2026-07-09 fix (Part 1): unbounded lifetime count removed.
    # `total_intents` is a permanent `None` in the payload —
    # operational visibility comes from 1h/24h + latest_ts + the
    # in-memory `intent_count` from runner_stats.
    total_intents = None

    latest_intent_age_s = _age_seconds(latest_intent_ts, now)
    # In-memory fallback for last-intent age when the cached doc is
    # silent (fresh brain / Atlas outage during first emit).
    if latest_intent_age_s is None and runner_stats:
        lh = runner_stats.get("loop_health") or {}
        latest_intent_age_s = lh.get("intent_last_success_age_s")

    # Seats lane-resolved from the live roster snapshot.
    snap = await _safe(get_roster(), None)
    if snap is None:
        atlas_partial = True
    assignments: Dict[str, Optional[str]] = (snap or {}).get("assignments") or {}
    seats_held = [
        {"seat": seat, "lane": _lane_of_role(seat)}
        for seat, occupant in assignments.items()
        if occupant == brain
    ]

    display_name = runner_stats.get("display_name") if runner_stats else brain.title()
    # 2026-02-19 (operator directive): identity split into two kinds.
    #   * `git_sha`     — DEPLOY identity (shared across brains, same repo)
    #   * `strategy_sha` — BRAIN identity (must differ per brain, one hash
    #                      per `shared/brains/<brain>/strategy.py`).
    # If two brains ever show the same `strategy_sha` at boot, the
    # `assert_no_strategy_collisions` guard in lifespan raises before
    # the app takes traffic. The 12-char sha256 prefix is compact
    # enough for the UI to render inline in a tile without wrapping.
    from shared.brains._strategy_identity import strategy_sha  # noqa: WPS433
    identity = {
        "app_name": "risedual-mc",
        "env_name": os.environ.get("ENV_NAME") or os.environ.get("ENVIRONMENT") or "preview",
        "git_sha": os.environ.get("GIT_SHA") or os.environ.get("RAILWAY_GIT_COMMIT_SHA") or "in-process",
        "strategy_sha": strategy_sha(brain),
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
            # 2026-02-20: DB-confirmed last write. Detects silent
            # write halts that the aggregate counts hide (see
            # comment near latest_intent lookup above).
            "latest_ts": latest_intent_ts,
            "latest_age_s": (
                round(latest_intent_age_s, 1)
                if latest_intent_age_s is not None else None
            ),
            "latest_symbol": latest_intent_symbol,
            "latest_action": latest_intent_action,
            "source": "brain_runtime_metrics",
            "atlas_partial": atlas_partial,
        },
        "write_health": await _build_write_health_for(brain, now),
        "in_process_runner": runner_stats,
    }


@router.get("/stack/status")
async def get_stack_status(
    _user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """One-read stack status for all four brains.

    2026-02-19 operator directive: the BrainConsole used to fire
    four independent `/admin/runtime/{brain}/status` calls, each
    of which fanned out into ~5 Atlas queries. Under Atlas load,
    all four consoles would show the SAME NetworkTimeout because
    they were hammering the same overloaded collection.

    This endpoint reads ONE compact stack document
    (`brain_runtime_metrics._id = risedual_stack`) with a single
    O(1) primary-key lookup. Frontend polls this once and slices
    the appropriate `brains.<name>` section locally.

    2026-02-20 doctrine ("3 clocks"): each brain section now carries
    THREE independent timestamps so the UI can diagnose which stage
    of the pipeline stopped:

        * `last_heartbeat_ts`                       — runner alive
        * `last_decision_ts`                        — decision produced
        * `last_db_confirmed_intent_ts`             — Mongo write OK
                                                     (HOLD included)
        * `last_db_confirmed_directional_intent_ts` — directional write OK

    plus counters (`decisions_total`, `intent_submit_attempts_total`,
    `intent_submit_successes_total`,
    `directional_submit_successes_total`,
    `intent_submit_failures_total`) and a derived `write_health`
    band (HEALTHY / STALE / DEAD / UNKNOWN / BLIND). `write_health`
    is session-aware for equity lanes — during closed-market hours
    an older directional write age relaxes the DEAD threshold
    because HOLD is a perfectly valid steady state overnight.

    Default-hostile: any Atlas failure returns `degraded=True`
    with an amber warning list — never a red banner.
    """
    from shared.brain_runtime_metrics import get_stack_status as _get_stack  # noqa: WPS433

    doc = await _get_stack()
    now = datetime.now(timezone.utc)
    if doc is None:
        return {
            "ok": True,
            "degraded": True,
            "stack_status": "unknown",
            "brains": {},
            "warnings": ["stack_status_temporarily_unavailable"],
            "now": now.isoformat(),
        }
    brains_in: Dict[str, Any] = doc.get("brains") or {}
    equity_open = _equity_market_open(now)
    brains_out: Dict[str, Any] = {}
    for name, section in brains_in.items():
        brains_out[name] = _decorate_brain_section(section or {}, now, equity_open)
    return {
        "ok": True,
        "degraded": False,
        "stack_status": doc.get("stack_status") or "healthy",
        "brains": brains_out,
        "equity_market_open": equity_open,
        "updated_at": doc.get("updated_at"),
        "first_seen_at": doc.get("first_seen_at"),
        "now": now.isoformat(),
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
        # 2026-02-19 fail-soft: return an AMBER `degraded=True`
        # payload with `ok=true` and warnings — NOT `ok=false` with
        # a red `error_detail` banner. A slow Atlas read on the
        # heavy status builder must never make the operator think
        # the brain itself is down. The frontend renders `degraded`
        # as an amber notice next to the still-live heartbeat card.
        return {
            "brain": brain,
            "ok": True,
            "degraded": True,
            "_proxied_from": "in_process",
            "warnings": ["intent_metrics_temporarily_unavailable"],
            "warn_detail": f"{type(exc).__name__}: {str(exc)[:200]}",
            "doctrine": "in_process_runtime_status",
            "ts": _now().isoformat(),
            "payload": {
                "brain": brain,
                "heartbeat": {"alive": True, "degraded_read": True},
                "intents": {"latest": None, "last_1h": None, "last_24h": None},
            },
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
