"""Brain-Health composite endpoint — `GET /api/admin/runtime/brain-health/{brain}`

Operator pattern (2026-02-17): every brain pod redeploy currently
requires poking three independent surfaces to confirm the wire-up
landed:
  1. `/api/admin/runtime/sidecar-checkin/{brain}` (identity stamp)
  2. `/api/admin/opinion-silence-watchdog/status`  (opinion freshness)
  3. seat-by-seat introspection of `sovereign_audit_log`        (seat walk)

This module collapses those three into a single read-only composite
keyed by brain. The operator runs ONE curl post-redeploy; the
admin dashboard tile renders the result as a single colored dot.

Doctrine pins:
  - READ-ONLY. Joins existing collections; never writes. Never
    serves broker keys. Never affects execution authority.
  - The green/degraded/dead verdict thresholds are RETURNED IN THE
    PAYLOAD so the tile, automated alerters, and future LLM
    summarisers all read the same numbers without grepping source.
  - Seat-walk is LANE-SCOPED. A brain that holds equity_governor
    but NOT crypto_governor must show a fresh equity walk and a
    null crypto walk — NOT a single "governor walk" that masks
    a half-dead lane (operator's explicit request 2026-02-17).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Path

from auth import get_current_user
from db import db
from namespaces import (
    LIVE_RUNTIMES,
    SIDECAR_CHECKINS,
    SHARED_OPINIONS,
    SOVEREIGN_AUDIT_LOG,
)


logger = logging.getLogger("risedual.brain_health")
router = APIRouter(prefix="/admin/runtime", tags=["brain-health"])


# ─────────────────── Doctrine thresholds ───────────────────
# Locked in the response payload so the tile + any future alerter
# read from the same source of truth. The operator can audit the
# verdict without grepping MC source.
THRESHOLDS: Dict[str, int] = {
    "checkin_max_age_s":   300,   # 5min — sidecar reposts every 5min
    "opinion_max_age_s":   900,   # 15min — opinion-heartbeat threshold
    "seat_walk_max_age_s": 1800,  # 30min — sovereign-audit cadence
}

# Lane → seat names that count as "active on this lane" per role.
# Equity council uses bare names; crypto council uses `crypto_*` twins.
# 2026-05-26 governor-exclusivity doctrine is enforced upstream in the
# roster; this map is purely a string lookup for join queries.
_LANE_SEATS: Dict[str, Dict[str, str]] = {
    "strategist": {"equity": "strategist", "crypto": "crypto_strategist"},
    "executor":   {"equity": "executor",   "crypto": "crypto"},  # crypto exec lives in `crypto` seat
    "governor":   {"equity": "governor",   "crypto": "crypto_governor"},
    "auditor":    {"equity": "auditor",    "crypto": "crypto_auditor"},
}
_ROLES: tuple[str, ...] = ("strategist", "executor", "governor", "auditor")
_LANES: tuple[str, ...] = ("equity", "crypto")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _age_seconds(iso_ts: Optional[str], now: datetime) -> Optional[float]:
    """Coerce an ISO-8601 timestamp into seconds-since. None on parse fail."""
    if not iso_ts:
        return None
    try:
        ts = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    return round((now - ts).total_seconds(), 1)


# ─────────────────── Per-surface gather helpers ───────────────────


async def _gather_checkin(brain: str, now: datetime) -> Dict[str, Any]:
    """Read sidecar_checkins for this brain. Compact view."""
    doc = await db[SIDECAR_CHECKINS].find_one(
        {"runtime": brain}, {"_id": 0},
    )
    if not doc:
        return {
            "verdict": "never",
            "env_name": None,
            "broker_mode": None,
            "process_identity": None,
            "last_checkin_at": None,
            "age_sec": None,
            "freshness": "never",
            "policy_hash_match": False,
        }
    last = doc.get("last_checkin_at")
    age = _age_seconds(last, now)
    if age is None:
        freshness = "never"
    elif age <= THRESHOLDS["checkin_max_age_s"]:
        freshness = "fresh"
    elif age <= THRESHOLDS["checkin_max_age_s"] * 6:  # ≤30m
        freshness = "stale"
    else:
        freshness = "dead"
    stamp = doc.get("stamp") or {}
    return {
        "verdict": doc.get("verdict", "invalid"),
        "env_name": stamp.get("env_name"),
        "broker_mode": stamp.get("broker_mode"),
        "process_identity": stamp.get("process_identity"),
        "last_checkin_at": last,
        "age_sec": age,
        "freshness": freshness,
        "policy_hash_match": bool(doc.get("policy_hash_match", False)),
    }


async def _gather_opinion(brain: str, now: datetime) -> Dict[str, Any]:
    """Last opinion POST timestamp for this brain. The opinion-silence
    watchdog already has this logic but we replicate the read here
    (single query, no service-layer hop) for endpoint locality."""
    row = await db[SHARED_OPINIONS].find_one(
        {"runtime": brain},
        {"_id": 0, "posted_at": 1, "opinion_id": 1, "symbol": 1},
        sort=[("posted_at", -1)],
    )
    if not row or not row.get("posted_at"):
        return {
            "last_posted_at": None,
            "last_opinion_id": None,
            "last_symbol": None,
            "age_sec": None,
            "silent": True,
            "kind": "never",
        }
    age = _age_seconds(row.get("posted_at"), now)
    silent = age is None or age > THRESHOLDS["opinion_max_age_s"]
    return {
        "last_posted_at": row.get("posted_at"),
        "last_opinion_id": row.get("opinion_id"),
        "last_symbol": row.get("symbol"),
        "age_sec": age,
        "silent": silent,
        "kind": "never" if age is None else ("stale" if silent else "fresh"),
    }


async def _gather_data_keys(brain: str, now: datetime) -> Dict[str, Any]:
    """Last market-data-keys proxy fetch. Confirms the brain's
    data-pipeline is still calling MC for keys."""
    row = await db["market_data_key_fetches"].find_one(
        {"brain": brain}, {"_id": 0}, sort=[("ts", -1)],
    )
    # Count last-24h fetches for a chattiness signal.
    cutoff = now.timestamp() - 86400
    # We can't filter on epoch (ts is ISO) without scanning, so just
    # count over the last 100 rows — bounded work, reasonable signal.
    recent_rows = await db["market_data_key_fetches"].find(
        {"brain": brain}, {"_id": 0, "ts": 1},
    ).sort("ts", -1).limit(500).max_time_ms(15000).to_list(length=500)
    fetch_count_24h = 0
    for r in recent_rows:
        ts_iso = r.get("ts")
        if not ts_iso:
            continue
        try:
            ts = datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue
        if ts.timestamp() >= cutoff:
            fetch_count_24h += 1
    if not row:
        return {
            "last_fetch_ts": None,
            "age_sec": None,
            "served_fields": [],
            "fetch_count_24h": 0,
        }
    return {
        "last_fetch_ts": row.get("ts"),
        "age_sec": _age_seconds(row.get("ts"), now),
        "served_fields": row.get("served_fields") or [],
        "fetch_count_24h": fetch_count_24h,
    }


async def _gather_seat_walk(brain: str, now: datetime) -> Dict[str, Any]:
    """Per-role × per-lane seat-walk freshness.

    Doctrine pin (2026-02-17 operator contract): cells are null when
    the brain is NOT CURRENTLY seated on that role × lane. Historical
    walks from a previous seat assignment are intentionally suppressed
    — operator wants the tile to answer "is the brain healthy in its
    CURRENT role" not "has this brain ever walked this seat". A
    half-dead governor (e.g., crypto_governor seated but never
    walking) is the bug pattern this contract catches.

    For seats THIS brain currently holds (per `shared.roster.get_roster`):
        cell = {ts, age_sec, stale, mode, seat}
    For seats NOT held:
        cell = None  (frontend renders as dimmed "not seated" dot)
    """
    from shared.roster import get_roster

    try:
        snap = await get_roster()
        assignments: Dict[str, Optional[str]] = (snap or {}).get("assignments") or {}
    except Exception:  # noqa: BLE001 — roster fetch is best-effort
        logger.warning("seat_walk: roster fetch failed; defaulting to all-null cells")
        assignments = {}

    # Invert: which seats does THIS brain currently hold?
    held_seats: set[str] = {
        seat for seat, occupant in assignments.items() if occupant == brain
    }

    out: Dict[str, Dict[str, Optional[Dict[str, Any]]]] = {}
    threshold = THRESHOLDS["seat_walk_max_age_s"]
    for role in _ROLES:
        out[role] = {}
        for lane in _LANES:
            seat = _LANE_SEATS[role][lane]
            if seat not in held_seats:
                # Brain doesn't currently hold this (role, lane) seat
                # → null per operator contract. Frontend dims the dot.
                out[role][lane] = None
                continue
            row = await db[SOVEREIGN_AUDIT_LOG].find_one(
                {"brain": brain, "posted_as": seat},
                {"_id": 0, "ts": 1, "mode": 1, "posted_as": 1},
                sort=[("ts", -1)],
            )
            if not row or not row.get("ts"):
                # Currently seated but has NEVER walked the seat —
                # this is the "freshly seated, hasn't fired yet"
                # state. Surface as stale-with-no-ts so the operator
                # sees a yellow dot rather than missing it.
                out[role][lane] = {
                    "ts": None,
                    "age_sec": None,
                    "stale": True,
                    "mode": None,
                    "seat": seat,
                }
                continue
            age = _age_seconds(row.get("ts"), now)
            stale = age is None or age > threshold
            out[role][lane] = {
                "ts": row.get("ts"),
                "age_sec": age,
                "stale": stale,
                "mode": row.get("mode"),
                "seat": seat,
            }
    return out


# ─────────────────── Verdict logic ───────────────────


def _compute_overall(
    checkin: Dict[str, Any],
    opinion: Dict[str, Any],
    seat_walk: Dict[str, Dict[str, Optional[Dict[str, Any]]]],
    emissions: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Distill the three surfaces into a single green/degraded/dead
    verdict. The reasons list is also returned so the tile can show
    a tooltip explaining WHY a brain is degraded without the
    operator hunting through the payload.

    Rules:
      - dead: never-checked-in OR checkin age > 6 × checkin_max
      - degraded: checkin stale, OR opinion stale-while-seated, OR
                  any HELD seat (non-null cell) has gone stale
      - green: all of the above pass
    """
    reasons: list[str] = []

    # 1) checkin — required for every brain
    if checkin.get("verdict") == "never":
        reasons.append("checkin_never")
        return {"verdict": "dead", "reasons": reasons, "thresholds": THRESHOLDS}
    age = checkin.get("age_sec")
    if age is None:
        reasons.append("checkin_age_unknown")
    elif age > THRESHOLDS["checkin_max_age_s"] * 6:
        reasons.append(f"checkin_dead_{int(age)}s")
        return {"verdict": "dead", "reasons": reasons, "thresholds": THRESHOLDS}
    elif age > THRESHOLDS["checkin_max_age_s"]:
        reasons.append(f"checkin_stale_{int(age)}s")
    if checkin.get("verdict") not in ("prod", "never"):
        # preview / policy_drift / invalid — degraded but not dead.
        reasons.append(f"checkin_verdict_{checkin['verdict']}")

    # 2) opinion — only material if this brain has at least one
    # opinion-producing seat. Executors route orders, they don't opine;
    # crypto-executor seats ditto. Doctrine pin (2026-02-17): silence
    # on a brain whose ONLY seat is executor is correct behavior, not
    # a regression — the operator's brain-health tile must not red-flag
    # Alpha as "opinion silent" while it sits in the executor chair.
    opinion_producing_seat_roles = {"strategist", "governor", "auditor", "advisor"}
    held_opinion_seat = any(
        cell is not None
        for role, lanes in seat_walk.items()
        for cell in lanes.values()
        if role in opinion_producing_seat_roles
    )
    if held_opinion_seat and opinion.get("silent"):
        age_op = opinion.get("age_sec")
        if age_op is None:
            reasons.append("opinion_never")
        else:
            reasons.append(f"opinion_silent_{int(age_op)}s")

    # 3) per-held-seat walk freshness — only flag for seats this
    # brain ACTUALLY holds (non-null cells).
    for role, lanes in seat_walk.items():
        for lane, cell in lanes.items():
            if cell is None:
                continue
            if cell.get("stale"):
                age_walk = cell.get("age_sec")
                reasons.append(
                    f"{role}_{lane}_stale_{int(age_walk) if age_walk else 'never'}s"
                )

    verdict = "green" if not reasons else "degraded"

    # 4) emissions — added 2026-02-23 to surface the Barracuda prod
    # regression. A brain that's checked in + opinion-fresh but has
    # written ZERO intents in 24h is silently broken (the worker
    # process is alive but its emit loop isn't running). Only flag
    # this for brains that hold an opinion-producing seat — pure
    # executors don't emit, by doctrine.
    if emissions and held_opinion_seat:
        if emissions.get("silent_24h"):
            reasons.append("emissions_silent_24h")
            verdict = "degraded"
        elif emissions.get("silent_1h"):
            reasons.append("emissions_silent_1h")
            # 1h silence is informational — don't auto-degrade
            # (could be a quiet pre-market hour). Tile can highlight.

    return {"verdict": verdict, "reasons": reasons, "thresholds": THRESHOLDS}


# ─────────────────── Routes ───────────────────


async def _gather_emissions(brain: str, now) -> Dict[str, Any]:
    """Count this brain's intent emissions over 1h + 24h windows.

    Added 2026-02-23 — operator-reported regression: Barracuda in prod
    was showing CHECKIN ✓ · OPINION fresh but 0 intents on the
    dashboard. The smoking-gun signal (`strategist_equity_stale_nevers`
    in the `WHY` text) was buried; with explicit emission counters on
    the BrainHealth response, a brain whose worker has silently
    stopped processing its seat shows up as `EMISSIONS 1h: 0` next to
    the other green indicators — impossible to miss.

    Reads `shared_intents` keyed on `stack_canonical` (post Phase C
    migration; the canonical-aware field is authoritative).
    """
    from db import db as _db  # noqa: WPS433
    from datetime import timedelta as _td  # noqa: WPS433
    cutoff_1h = (now - _td(hours=1)).isoformat()
    cutoff_24h = (now - _td(hours=24)).isoformat()
    n_1h = await _db["shared_intents"].count_documents({
        "stack_canonical": brain, "ingest_ts": {"$gte": cutoff_1h},
    })
    n_24h = await _db["shared_intents"].count_documents({
        "stack_canonical": brain, "ingest_ts": {"$gte": cutoff_24h},
    })
    # `silent_1h_during_market` is intentionally informational, not a
    # hard verdict driver — market hours can change between runs and
    # the underlying RTH check lives in shared/market_hours.py. The
    # dashboard tile can decide how to highlight zero counts; we just
    # surface them with the same shape as `opinion.silent`.
    return {
        "intents_1h": int(n_1h),
        "intents_24h": int(n_24h),
        "silent_1h": n_1h == 0,
        "silent_24h": n_24h == 0,
    }


@router.get("/brain-health/{brain}")
async def get_brain_health(
    brain: str = Path(..., description="brain id — alpha|camaro|chevelle|redeye"),
    _user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """Composite health for a single brain. Read-only join across
    `sidecar_checkins`, `shared_opinions`, `market_data_key_fetches`,
    and `sovereign_audit_log`.

    Returns the operator-pinned payload contract (2026-02-17):
        {
          brain, checked_at,
          checkin:    { verdict, env_name, broker_mode, process_identity,
                        last_checkin_at, age_sec, freshness,
                        policy_hash_match },
          opinion:    { last_posted_at, last_opinion_id, last_symbol,
                        age_sec, silent, kind },
          data_keys:  { last_fetch_ts, age_sec, served_fields,
                        fetch_count_24h },
          seat_walk:  { role: { lane: {ts, age_sec, stale, mode, seat}
                                | null } },
          overall:    { verdict: green|degraded|dead, reasons[],
                        thresholds: { ... } }
        }
    """
    brain = (brain or "").lower().strip()
    if brain not in LIVE_RUNTIMES:
        raise HTTPException(status_code=404, detail=f"unknown brain {brain!r}")

    now = _now()
    checkin = await _gather_checkin(brain, now)
    opinion = await _gather_opinion(brain, now)
    data_keys = await _gather_data_keys(brain, now)
    seat_walk = await _gather_seat_walk(brain, now)
    overall = _compute_overall(checkin, opinion, seat_walk)

    return {
        "brain": brain,
        "checked_at": now.isoformat(),
        "checkin": checkin,
        "opinion": opinion,
        "data_keys": data_keys,
        "seat_walk": seat_walk,
        "overall": overall,
        "doctrine": "operator_read_only_composite",
    }


@router.get("/brain-health")
async def list_brain_health(
    _user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """Fleet-wide composite — one row per LIVE_RUNTIME. Same shape
    per brain as the singleton endpoint. This is what the dashboard
    tile reads on its 15s tick so the operator sees all 4 brains
    side-by-side in one fetch.
    """
    now = _now()
    rows: Dict[str, Dict[str, Any]] = {}
    for brain in LIVE_RUNTIMES:
        checkin = await _gather_checkin(brain, now)
        opinion = await _gather_opinion(brain, now)
        data_keys = await _gather_data_keys(brain, now)
        seat_walk = await _gather_seat_walk(brain, now)
        emissions = await _gather_emissions(brain, now)  # 2026-02-23
        overall = _compute_overall(checkin, opinion, seat_walk, emissions)
        rows[brain] = {
            "brain": brain,
            "checkin": checkin,
            "opinion": opinion,
            "data_keys": data_keys,
            "seat_walk": seat_walk,
            "emissions": emissions,    # 2026-02-23 — explicit silence signal
            "overall": overall,
        }
    return {
        "checked_at": now.isoformat(),
        "brains": rows,
        "thresholds": THRESHOLDS,
        "doctrine": "operator_read_only_composite",
    }
