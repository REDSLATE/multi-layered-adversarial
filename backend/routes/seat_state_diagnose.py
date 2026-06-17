"""Seat-state ground-truth diagnostic — one endpoint, all sources.

The user kept seeing "STRATEGIST · holder: vacant" in the intent feed
even after assigning brains via Quick Seat Switches. Root cause: this
codebase has FOUR independent seat-holder storage backends, and the
intent UI reads from the legacy two while the new Paradox v2 pipeline
reads from the new two.

This endpoint dumps EVERY source side-by-side so an operator can see
the drift in one JSON response, then decide whether to heal.

Sources covered:
    1. brain_roster.assignments              (legacy, writes from QSS)
    2. shared_executor_seat doc              (legacy executor pin)
    3. shared_auditor_seat doc               (legacy auditor pin)
    4. paradox_v2_seat_policy_config         (new pipeline policy)
    5. paradox_v2_seat_trusted_brains        (new pipeline trust list)

Read-only. No writes. Safe to hit on prod.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List

from fastapi import APIRouter, Depends

from auth import get_current_user
from db import db
from namespaces import (
    BRAIN_ROSTER,
    PARADOX_V2_SEAT_POLICY,
    PARADOX_V2_SEAT_TRUSTED,
    SHARED_AUDITOR_SEAT,
    SHARED_EXECUTOR_SEAT,
)


router = APIRouter(prefix="/admin/seat-state", tags=["seat-state-diagnose"])


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@router.get("/all-sources")
async def all_sources(_user: dict = Depends(get_current_user)) -> Dict[str, Any]:
    """Return every storage backend's view of the seat assignments.

    Each "row" is a canonical lane+role. Each column is a storage
    source. The `agreement` field flags which sources agree and which
    drift.
    """
    # ── source #1: legacy brain_roster ──────────────────────────────
    roster = await db[BRAIN_ROSTER].find_one({"_id": "current"}, {"_id": 0}) or {}
    roster_assignments: Dict[str, Any] = roster.get("assignments") or {}

    # ── source #2: legacy executor pin doc ──────────────────────────
    exec_doc = await db[SHARED_EXECUTOR_SEAT].find_one(
        {"_id": "executor"}, {"_id": 0},
    ) or {}

    # ── source #3: legacy auditor pin doc ───────────────────────────
    audit_doc = await db[SHARED_AUDITOR_SEAT].find_one(
        {"_id": "auditor"}, {"_id": 0},
    ) or {}

    # ── source #4: paradox v2 seat policy ───────────────────────────
    policy_rows: List[Dict[str, Any]] = []
    async for r in db[PARADOX_V2_SEAT_POLICY].find({}, {"_id": 0}):
        policy_rows.append(r)
    policy_by_seat = {r["seat_id"]: r for r in policy_rows if r.get("seat_id")}

    # ── source #5: paradox v2 trust list ────────────────────────────
    trust_by_seat: Dict[str, List[str]] = {}
    async for t in db[PARADOX_V2_SEAT_TRUSTED].find({}, {"_id": 0}):
        seat = t.get("seat_id")
        if not seat:
            continue
        trust_by_seat.setdefault(seat, []).append(t.get("brain_id"))

    # ── canonical lane + role grid ──────────────────────────────────
    grid: List[Dict[str, Any]] = []
    for lane, roles in (
        ("equity", ["strategist", "executor", "governor", "auditor"]),
        ("crypto", ["strategist", "executor", "governor", "auditor"]),
    ):
        for role in roles:
            roster_key = role if lane == "equity" else f"crypto_{role}"
            # The legacy roster uses "crypto" (not "crypto_executor")
            # for the crypto executor. Handle both keys.
            if lane == "crypto" and role == "executor":
                roster_says = (
                    roster_assignments.get("crypto_executor")
                    or roster_assignments.get("crypto")
                )
            else:
                roster_says = roster_assignments.get(roster_key)

            # Auditor is special — both legacy and audit pin doc
            audit_pin_says = audit_doc.get("holder") if (lane == "equity" and role == "auditor") else None
            exec_pin_says = exec_doc.get("holder") if (lane == "equity" and role == "executor") else None

            v2_seat_id = f"{lane}_{role}"
            v2_policy = policy_by_seat.get(v2_seat_id)
            v2_trust = sorted([b for b in trust_by_seat.get(v2_seat_id, []) if b]) or []

            # Sources where this seat IS represented and what they say.
            sources_say: Dict[str, Any] = {
                "brain_roster":         roster_says,
                "shared_executor_seat": exec_pin_says,
                "shared_auditor_seat":  audit_pin_says,
                "v2_policy_enabled":    bool(v2_policy.get("enabled")) if v2_policy else None,
                "v2_trusted_brains":    v2_trust if v2_policy else None,
            }

            # Agreement: holder values that are not None.
            holder_values = [
                v for k, v in sources_say.items()
                if k in ("brain_roster", "shared_executor_seat", "shared_auditor_seat")
                and v
            ]
            unique_holders = sorted(set(holder_values))
            v2_trust_set = set(v2_trust or [])
            v2_in_sync = (
                not unique_holders or
                (len(unique_holders) == 1 and unique_holders[0] in v2_trust_set)
            )

            grid.append({
                "lane": lane,
                "role": role,
                "v2_seat_id": v2_seat_id,
                "sources_say": sources_say,
                "unique_legacy_holders": unique_holders,
                "v2_trust_in_sync": v2_in_sync if unique_holders else None,
                "drift_detected": len(unique_holders) > 1 or (
                    len(unique_holders) == 1
                    and v2_policy is not None
                    and v2_trust_set
                    and unique_holders[0] not in v2_trust_set
                ),
            })

    # ── top-level summary ───────────────────────────────────────────
    drift_seats = [g for g in grid if g["drift_detected"]]
    vacant_in_roster = [
        g for g in grid
        if not g["unique_legacy_holders"]
    ]
    return {
        "as_of": _now(),
        "summary": {
            "total_seats": len(grid),
            "drift_seats_count": len(drift_seats),
            "vacant_in_roster_count": len(vacant_in_roster),
            "drift_seats": [f"{g['lane']}/{g['role']}" for g in drift_seats],
            "vacant_in_roster": [f"{g['lane']}/{g['role']}" for g in vacant_in_roster],
        },
        "raw_sources": {
            "brain_roster_assignments": roster_assignments,
            "shared_executor_seat": exec_doc,
            "shared_auditor_seat": audit_doc,
            "paradox_v2_seat_policy_count": len(policy_rows),
            "paradox_v2_trust_pairs": [
                {"seat_id": s, "brains": sorted(b for b in bs if b)}
                for s, bs in sorted(trust_by_seat.items())
            ],
        },
        "grid": grid,
        "diagnosis_legend": {
            "brain_roster":         "legacy — POST /api/admin/roster/assign writes here. QSS UI calls this.",
            "shared_executor_seat": "legacy executor pin. Read by intent gate. Should match brain_roster.executor.",
            "shared_auditor_seat":  "legacy auditor pin. Read by intent gate auditor display.",
            "v2_policy_enabled":    "Paradox v2 seat policy — does this seat exist in the new pipeline at all.",
            "v2_trusted_brains":    "Paradox v2 trust list — which brains the new pipeline allows for this seat.",
        },
        "fix_paths": {
            "if_drift": (
                "Two legacy sources disagree. Either POST /api/executor/rotate "
                "to align the legacy executor doc, or wipe the doc so the gate "
                "falls back to brain_roster."
            ),
            "if_vacant_in_roster_but_qss_says_filled": (
                "POST /api/admin/roster/assign with {role: '<role>', brain: '<brain>', "
                "reason: 'heal_from_qss'} for the missing rows. The UI button "
                "may have failed silently on a prior session."
            ),
            "if_v2_trust_missing": (
                "POST /api/admin/paradox_v2/trusted-brains with {seat_id, brain_id}. "
                "Required before the unified pipeline (UNIFIED_PIPELINE_ENABLED=true) "
                "can call the broker for that brain."
            ),
        },
    }
