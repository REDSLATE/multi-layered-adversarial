"""Brain Emission Diagnostic — answer "why is this brain silent?".

Doctrine pin (2026-02-18):
    Read-only. Surfaces the seven hypotheses the operator asks every
    time a brain stops producing routable intents. Never mutates.
    Never gates execution. Returns typed `silent_reasons` so the
    operator can act without reading a hundred lines of logs.

Hypotheses surfaced per brain (alpha, camaro, chevelle, redeye):

    NO_HEARTBEAT_EVER             never pinged MC
    HEARTBEAT_DEAD                last heartbeat > 30 min
    HEARTBEAT_STALE               last heartbeat 5-30 min
    NO_SIDECAR_CHECKIN            never POSTed RuntimeStamp
    SIDECAR_CHECKIN_DRIFT         stamp present but policy_hash drift
    NO_INTENT_EVER                shared_intents has zero rows for stack
    NO_INTENT_LAST_24H            has history but silent today
    ONLY_HOLD_ACTIONS             emits HOLDs but no BUY/SELL/SHORT/COVER
    NO_EXECUTOR_SEAT_FOR_LANE     brain doesn't hold an execute-seat
                                  for any lane → BUY/SELL would fail
                                  the executor_seat_check gate anyway
    ALL_INTENTS_REJECTED_AT_INGEST   audit-only rows dominate
    PRODUCING_ROUTABLE_INTENTS    happy path; brain is doing its job

A brain can carry multiple reasons (e.g., ONLY_HOLD_ACTIONS +
NO_EXECUTOR_SEAT_FOR_LANE means "even if it tried, it couldn't").

Endpoints:
    GET /api/admin/brain/emission-diagnose
        Returns all four brains in one shape.

    GET /api/admin/brain/emission-diagnose/{brain}
        Single-brain detail.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from auth import get_current_user
from db import db
from namespaces import (
    DISCUSSION_PARTICIPANTS,
    RUNTIMES,
    SHARED_HEARTBEATS,
    SHARED_INTENTS,
    SIDECAR_CHECKINS,
    SOVEREIGN_STATE,
)


router = APIRouter(prefix="/admin/brain", tags=["brain-diagnose"])


# ─────────────────────────── helpers ───────────────────────────


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def _age_seconds(iso: Optional[str]) -> Optional[float]:
    if not iso:
        return None
    try:
        t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    return round((_now() - t).total_seconds(), 1)


def _heartbeat_band(age: Optional[float]) -> str:
    if age is None:
        return "never"
    if age < 90:
        return "fresh"
    if age < 300:
        return "ok"
    if age < 1800:
        return "stale"
    return "dead"


# ─────────────────── per-brain assembly ────────────────────────


async def _heartbeat_status(brain: str) -> dict:
    hb = await db[SHARED_HEARTBEATS].find_one({"runtime": brain}, {"_id": 0})
    sv = await db[SOVEREIGN_STATE].find_one({"brain": brain}, {"_id": 0})
    hb_iso = (hb or {}).get("last_seen")
    sv_iso = (sv or {}).get("updated_at")
    hb_age = _age_seconds(hb_iso)
    sv_age = _age_seconds(sv_iso)
    return {
        "last_heartbeat_at": hb_iso,
        "heartbeat_age_seconds": hb_age,
        "heartbeat_band": _heartbeat_band(hb_age),
        "last_contribution_at": sv_iso,
        "contribution_age_seconds": sv_age,
        "ever_heartbeated": hb_iso is not None,
        "ever_contributed": sv_iso is not None,
    }


async def _sidecar_checkin_status(brain: str) -> dict:
    doc = await db[SIDECAR_CHECKINS].find_one({"runtime": brain}, {"_id": 0})
    if not doc:
        return {
            "ever_checked_in": False,
            "verdict": "never",
            "last_checkin_at": None,
            "checkin_age_seconds": None,
            "policy_hash_match": None,
            "errors": [],
        }
    return {
        "ever_checked_in": True,
        "verdict": doc.get("verdict"),
        "last_checkin_at": doc.get("last_checkin_at"),
        "checkin_age_seconds": _age_seconds(doc.get("last_checkin_at")),
        "policy_hash_match": doc.get("policy_hash_match"),
        "errors": (doc.get("validation") or {}).get("errors", []),
    }


async def _roster_seats_for_brain(brain: str) -> dict:
    """Which seats does this brain currently hold, and does any of
    them grant execute authority for equity or crypto?"""
    from shared.roster import get_roster
    from shared.executor_seat import seats_with_execute

    roster = await get_roster()
    assignments = (roster or {}).get("assignments") or {}
    held = [seat for seat, holder in assignments.items() if holder == brain]

    equity_seats = set(seats_with_execute("equity"))
    crypto_seats = set(seats_with_execute("crypto"))

    holds_equity_executor = any(s in equity_seats for s in held)
    holds_crypto_executor = any(s in crypto_seats for s in held)

    return {
        "seats_held": held,
        "holds_equity_executor": holds_equity_executor,
        "holds_crypto_executor": holds_crypto_executor,
        "equity_execute_seats": sorted(equity_seats),
        "crypto_execute_seats": sorted(crypto_seats),
    }


async def _intent_emission_stats(brain: str, hours: int = 24) -> dict:
    """Counts + last-seen timestamps for this brain's shared_intents.

    Splits by `action` and `gate_state` so the operator can see at
    a glance whether the brain is producing HOLD-only, all-rejected,
    or healthy directional traffic.
    """
    since = (_now() - timedelta(hours=hours)).isoformat()
    coll = db[SHARED_INTENTS]

    # All-time row count for this stack.
    total_ever = await coll.count_documents({"stack": brain})

    # Window-scoped rows.
    win_q = {"stack": brain, "ingest_ts": {"$gte": since}}
    window_total = await coll.count_documents(win_q)

    # Per-action counts (window).
    by_action: dict = {}
    for action in ("BUY", "SELL", "SHORT", "COVER", "HOLD"):
        by_action[action] = await coll.count_documents({**win_q, "action": action})

    # Per-gate-state counts (window).
    by_gate_state: dict = {}
    for gs in ("pending", "passed", "blocked", "dry_run_passed",
               "dry_run_blocked", "rejected_at_ingest"):
        by_gate_state[gs] = await coll.count_documents({**win_q, "gate_state": gs})

    # Per-lane counts (window).
    by_lane: dict = {}
    for lane in ("equity", "crypto", None):
        if lane is None:
            by_lane["unset"] = await coll.count_documents(
                {**win_q, "lane": None}
            )
        else:
            by_lane[lane] = await coll.count_documents({**win_q, "lane": lane})

    # Latest emission overall + latest DIRECTIONAL emission.
    latest = await coll.find_one(
        {"stack": brain}, {"_id": 0, "action": 1, "symbol": 1,
                           "lane": 1, "gate_state": 1, "ingest_ts": 1},
        sort=[("ingest_ts", -1)],
    )
    latest_directional = await coll.find_one(
        {"stack": brain, "action": {"$in": ["BUY", "SELL", "SHORT", "COVER"]}},
        {"_id": 0, "action": 1, "symbol": 1, "lane": 1,
         "gate_state": 1, "ingest_ts": 1},
        sort=[("ingest_ts", -1)],
    )

    # Audit-only rejection rows (lane-policy or other ingest blocks).
    audit_only_count = await coll.count_documents(
        {"stack": brain, "audit_only": True, "ingest_ts": {"$gte": since}}
    )

    return {
        "window_hours": hours,
        "since": since,
        "total_intents_ever": total_ever,
        "window_total": window_total,
        "by_action": by_action,
        "by_gate_state": by_gate_state,
        "by_lane": by_lane,
        "audit_only_rejections_in_window": audit_only_count,
        "latest_emission": latest,
        "latest_directional_emission": latest_directional,
        "latest_directional_age_seconds": (
            _age_seconds((latest_directional or {}).get("ingest_ts"))
        ),
    }


def _classify_silent_reasons(
    *, brain: str,
    heartbeat: dict,
    checkin: dict,
    roster: dict,
    emission: dict,
) -> list[str]:
    """Apply the typed-reason ladder. Order matters: most fundamental
    failure first (no heartbeat) so the operator fixes upstream issues
    before drilling into emission shape."""
    reasons: list[str] = []

    # ── Liveness ─────────────────────────────────────────────────────
    if not heartbeat["ever_heartbeated"]:
        reasons.append("NO_HEARTBEAT_EVER")
    elif heartbeat["heartbeat_band"] == "dead":
        reasons.append("HEARTBEAT_DEAD")
    elif heartbeat["heartbeat_band"] == "stale":
        reasons.append("HEARTBEAT_STALE")

    # ── Identity ─────────────────────────────────────────────────────
    if not checkin["ever_checked_in"]:
        reasons.append("NO_SIDECAR_CHECKIN")
    elif checkin["verdict"] == "policy_drift":
        reasons.append("SIDECAR_CHECKIN_DRIFT")
    elif checkin["verdict"] == "invalid":
        reasons.append("SIDECAR_CHECKIN_INVALID")
    elif checkin["verdict"] == "preview":
        reasons.append("SIDECAR_RUNNING_IN_PREVIEW")

    # ── Authority ────────────────────────────────────────────────────
    if not (roster["holds_equity_executor"] or roster["holds_crypto_executor"]):
        reasons.append("NO_EXECUTOR_SEAT_FOR_LANE")

    # ── Emission shape ───────────────────────────────────────────────
    if emission["total_intents_ever"] == 0:
        reasons.append("NO_INTENT_EVER")
    elif emission["window_total"] == 0:
        reasons.append("NO_INTENT_LAST_24H")
    else:
        ba = emission["by_action"]
        directional = ba["BUY"] + ba["SELL"] + ba["SHORT"] + ba["COVER"]
        if directional == 0 and ba["HOLD"] > 0:
            reasons.append("ONLY_HOLD_ACTIONS")
        if emission["audit_only_rejections_in_window"] > 0 and \
                emission["audit_only_rejections_in_window"] == emission["window_total"]:
            reasons.append("ALL_INTENTS_REJECTED_AT_INGEST")

    # ── Happy path ───────────────────────────────────────────────────
    if not reasons:
        reasons.append("PRODUCING_ROUTABLE_INTENTS")
    elif emission["latest_directional_emission"] is not None and \
            emission["latest_directional_age_seconds"] is not None and \
            emission["latest_directional_age_seconds"] < 3600:
        # Brain emitted SOMETHING routable within the last hour even
        # though it has other complaints — note that explicitly.
        reasons.append("RECENT_DIRECTIONAL_PRESENT")

    return reasons


async def _diagnose_one(brain: str, hours: int) -> dict:
    heartbeat = await _heartbeat_status(brain)
    checkin = await _sidecar_checkin_status(brain)
    roster = await _roster_seats_for_brain(brain)
    emission = await _intent_emission_stats(brain, hours=hours)
    silent_reasons = _classify_silent_reasons(
        brain=brain,
        heartbeat=heartbeat,
        checkin=checkin,
        roster=roster,
        emission=emission,
    )
    # Operator-readable summary line — one sentence the UI can render
    # next to the brain name.
    summary = _summarize(brain, silent_reasons, emission, roster)
    return {
        "brain": brain,
        "checked_at": _now_iso(),
        "summary": summary,
        "silent_reasons": silent_reasons,
        "heartbeat": heartbeat,
        "sidecar_checkin": checkin,
        "roster": roster,
        "emission": emission,
    }


def _summarize(brain: str, reasons: list[str], emission: dict,
               roster: dict) -> str:
    """One-line operator summary."""
    if "PRODUCING_ROUTABLE_INTENTS" in reasons:
        ba = emission["by_action"]
        directional = ba["BUY"] + ba["SELL"] + ba["SHORT"] + ba["COVER"]
        return (
            f"{brain} healthy — {directional} directional + {ba['HOLD']} "
            f"HOLD intents in last {emission['window_hours']}h."
        )
    if "NO_HEARTBEAT_EVER" in reasons or "NO_SIDECAR_CHECKIN" in reasons:
        return f"{brain} has never contacted MC — sidecar pod likely not running."
    if "HEARTBEAT_DEAD" in reasons:
        return f"{brain} sidecar heartbeat is dead — process crashed or pod gone."
    if "NO_INTENT_EVER" in reasons:
        return f"{brain} sidecar is alive but has NEVER posted an intent."
    if "NO_INTENT_LAST_24H" in reasons:
        return f"{brain} sidecar is alive but silent for the last 24h."
    if "ONLY_HOLD_ACTIONS" in reasons:
        ba = emission["by_action"]
        not_exec = "NO_EXECUTOR_SEAT_FOR_LANE" in reasons
        suffix = (
            " AND brain doesn't hold any execute-seat, so even directional "
            "intents would fail the executor_seat_check gate"
            if not_exec else ""
        )
        return (
            f"{brain} emitting {ba['HOLD']} HOLD-only intents in window — "
            f"brain is self-censoring directional calls{suffix}."
        )
    if "ALL_INTENTS_REJECTED_AT_INGEST" in reasons:
        return f"{brain} every intent is being rejected at ingest (audit-only rows)."
    return f"{brain} status: {', '.join(reasons)}"


# ─────────────────────────── endpoints ─────────────────────────


@router.get("/emission-diagnose")
async def diagnose_all(
    hours: int = Query(default=24, ge=1, le=168),
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    """All four brains in one shape. Loops over `RUNTIMES`."""
    rows = []
    for b in RUNTIMES:
        rows.append(await _diagnose_one(b, hours=hours))
    # Operator-helpful aggregate: which brains are healthy vs silent.
    healthy = [r["brain"] for r in rows
               if "PRODUCING_ROUTABLE_INTENTS" in r["silent_reasons"]]
    silent = [r["brain"] for r in rows
              if "PRODUCING_ROUTABLE_INTENTS" not in r["silent_reasons"]]
    return {
        "checked_at": _now_iso(),
        "window_hours": hours,
        "healthy_brains": healthy,
        "silent_brains": silent,
        "doctrine_note": (
            "Read-only diagnostic. Never blocks execution; never mutates "
            "state. A brain is 'silent' from MC's perspective; the actual "
            "sidecar process lives outside this repo."
        ),
        "rows": rows,
    }


@router.get("/emission-diagnose/{brain}")
async def diagnose_one(
    brain: str,
    hours: int = Query(default=24, ge=1, le=168),
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    """Single-brain detail."""
    b = brain.lower()
    if b not in DISCUSSION_PARTICIPANTS:
        raise HTTPException(
            status_code=404,
            detail=f"unknown brain {brain!r}; "
                   f"known: {list(DISCUSSION_PARTICIPANTS)}",
        )
    return await _diagnose_one(b, hours=hours)
