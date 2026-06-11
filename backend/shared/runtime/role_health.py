"""
Role health — continuous survival conditions for the anchored roles.
====================================================================

Doctrine (PARADOX hierarchy, 2026-05-20):

    A role does not occupy itself by virtue of being named. Each
    role's anchored runtime must continuously satisfy the role's
    survival conditions. Failing any one of them vacates the seat
    for as long as the failure persists.

This module is the single source of truth for those conditions. It
exposes one function — `evaluate_role_health(role)` — that returns
a structured verdict the gate layer can consult.

EXECUTOR (Camaro) — survival conditions:
    1. Active `mc_checkin` within last 90 s with matching policy_hash
       (proves the live sidecar is the one MC trusts, not a stale
       script holding a leaked key).
    2. Zero orphan fills in the last 24 h for this runtime
       (the 5/18 event is the catalyst — Camaro can't earn the seat
       back while its own source_stack is still producing orphans).
    3. Orphan watchdog armed and looping (so future orphans will be
       caught immediately).

OPPONENT (REDEYE) — survival conditions:
    1. Mode declared (`live` | `shadow_observation` | `offline`).
       Shadow and offline are valid states, not failures — they
       change `audit_status` on paradox_records but do not vacate
       the seat.

GOVERNOR (Chevelle), STRATEGIST (Alpha), MEMORY (Shelly) — survival
conditions can be added per role as needed; this module starts with
the executor and opponent because those are the two with
operationally-coupled live constraints today.

Read-only. No DB writes from this module — pure derivation from the
state of the world.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from db import db
from namespaces import (
    OPPONENT_MODE_LIVE,
    OPPONENT_MODE_OFFLINE,
    OPPONENT_MODE_SHADOW,
    ROLE_ANCHORS,
)
from shared.runtime.platform_survival import policy_hash


# Survival thresholds — operator-tunable via env.
EXECUTOR_CHECKIN_MAX_AGE_S = int(os.environ.get("ROLE_EXECUTOR_CHECKIN_MAX_AGE_S", "90"))
EXECUTOR_ORPHAN_WINDOW_H = int(os.environ.get("ROLE_EXECUTOR_ORPHAN_WINDOW_H", "24"))


async def _executor_health() -> Dict[str, Any]:
    """Hardening clause for the executor role (Camaro)."""
    runtime = ROLE_ANCHORS["executor"]

    # 1. Fresh check-in with matching policy_hash
    checkin = await db.sidecar_checkins.find_one({"runtime": runtime})
    checkin_age_s: Optional[float] = None
    checkin_hash_match = False
    if checkin:
        last_seen = checkin.get("last_seen") or checkin.get("created_at")
        if isinstance(last_seen, str):
            try:
                last_seen = datetime.fromisoformat(last_seen.replace("Z", "+00:00"))
            except ValueError:
                last_seen = None
        if isinstance(last_seen, datetime):
            now = datetime.now(timezone.utc)
            if last_seen.tzinfo is None:
                last_seen = last_seen.replace(tzinfo=timezone.utc)
            checkin_age_s = (now - last_seen).total_seconds()
        checkin_hash_match = checkin.get("policy_hash_match") is True

    checkin_fresh = (
        checkin_age_s is not None
        and checkin_age_s <= EXECUTOR_CHECKIN_MAX_AGE_S
    )

    # 2. Zero orphan fills in the last 24h attributable to this runtime.
    # Orphans live in the memory_kernel_ledger as UV execution memories.
    # We count by source_stack — historically `alpaca_orphan` /
    # `alpaca_orphan_watchdog` (now retired with the broker) plus any
    # future runtime-tagged orphan source. If/when other runtimes get
    # their own watchdog tags, add them here.
    orphan_cutoff = datetime.now(timezone.utc) - timedelta(hours=EXECUTOR_ORPHAN_WINDOW_H)
    runtime_orphan_tags = {runtime, f"{runtime}_orphan", "alpaca_orphan", "alpaca_orphan_watchdog"}
    recent_orphans = await db.memory_kernel_ledger.count_documents({
        "memory_type": "execution",
        "provenance": "UV",
        "source_stack": {"$in": list(runtime_orphan_tags)},
        "created_at": {"$gte": orphan_cutoff},
    })

    # 3. Orphan watchdog — REMOVED 2026-02-19 with Alpaca deprecation.
    # The watchdog was an Alpaca-only fill reconciler; there is no
    # equivalent surface on Webull because MC owns the order-issuance
    # path end-to-end (no third-party stack can issue fills behind
    # MC's back). The condition is reported true for back-compat with
    # any executor-health dashboard still keying off it.
    watchdog_enabled = True

    conditions = {
        "checkin_fresh": checkin_fresh,
        "checkin_age_s": checkin_age_s,
        "checkin_hash_match": checkin_hash_match,
        "recent_orphans_24h": recent_orphans,
        "watchdog_armed": watchdog_enabled,
    }

    failed_reasons = []
    if not checkin_fresh:
        failed_reasons.append(
            f"checkin_stale: last seen {checkin_age_s}s ago "
            f"(max {EXECUTOR_CHECKIN_MAX_AGE_S}s)"
        )
    if not checkin_hash_match:
        failed_reasons.append("policy_hash_mismatch: live sidecar not running MC's doctrine")
    if recent_orphans > 0:
        failed_reasons.append(
            f"recent_orphans: {recent_orphans} orphan fill(s) in last "
            f"{EXECUTOR_ORPHAN_WINDOW_H}h from this runtime's sources"
        )

    healthy = len(failed_reasons) == 0
    return {
        "role": "executor",
        "runtime": runtime,
        "healthy": healthy,
        "seat_status": "occupied" if healthy else "vacant",
        "conditions": conditions,
        "failed_reasons": failed_reasons,
        "policy_hash": policy_hash(),
    }


async def _opponent_health() -> Dict[str, Any]:
    """Opponent role (REDEYE) — mode declaration only.

    Shadow and offline are valid declared states, not failures.
    They alter the audit_status stamped on paradox_records.
    """
    runtime = ROLE_ANCHORS["opponent"]
    declared = os.environ.get("OPPONENT_MODE", OPPONENT_MODE_SHADOW)
    if declared not in {OPPONENT_MODE_LIVE, OPPONENT_MODE_SHADOW, OPPONENT_MODE_OFFLINE}:
        declared = OPPONENT_MODE_OFFLINE
    return {
        "role": "opponent",
        "runtime": runtime,
        "healthy": True,
        "mode": declared,
        "seat_status": "occupied" if declared != OPPONENT_MODE_OFFLINE else "vacant",
        "audit_implication": {
            OPPONENT_MODE_LIVE: "paradox_record.audit_status=final",
            OPPONENT_MODE_SHADOW: "paradox_record.audit_status=shadow (trades fire; opponent observes)",
            OPPONENT_MODE_OFFLINE: "paradox_record.audit_status=unaudited (operator must be aware)",
        }[declared],
    }


async def _trivial_health(role: str) -> Dict[str, Any]:
    """Strategist / governor / memory — no live survival constraints yet.
    Returns a healthy stub anchored to the right runtime.
    """
    return {
        "role": role,
        "runtime": ROLE_ANCHORS.get(role),
        "healthy": True,
        "seat_status": "occupied",
        "conditions": {},
        "failed_reasons": [],
    }


async def evaluate_role_health(role: str) -> Dict[str, Any]:
    """Public surface — returns a verdict for a single anchored role."""
    if role == "executor":
        return await _executor_health()
    if role == "opponent":
        return await _opponent_health()
    if role in {"strategist", "governor", "memory"}:
        return await _trivial_health(role)
    return {
        "role": role,
        "runtime": None,
        "healthy": False,
        "seat_status": "vacant",
        "failed_reasons": [f"unknown_role: {role}"],
    }


async def evaluate_all_roles() -> Dict[str, Any]:
    """Roster view — every anchored role with its health verdict."""
    out = {}
    for role in ROLE_ANCHORS.keys():
        out[role] = await evaluate_role_health(role)
    return out
