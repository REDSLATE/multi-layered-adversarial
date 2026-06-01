"""Seat-registry diagnostic — answer "is the gate seeing the right holder?".

Read-only. Surfaces the exact state the execution gate sees at this
moment for every canonical seat, both lanes, and both sources (legacy
executor_seat doc + roster.assignments). Highlights any drift between
them so the operator can spot split-brain instantly.

Doctrine pin (2026-02-17):
    The gate authoritatively reads `get_seat_holder(seat)`. This
    endpoint calls the SAME function for every canonical seat and
    reports what it returns. The operator never has to read code to
    learn what the gate sees.

Why this exists:
    Before this endpoint, the operator had to inspect TWO collections
    (`shared_executor_seat` + `shared_brain_roster`), run mental
    precedence rules, and cross-check against `seat_policy.SEAT_POLICY`
    to figure out why an intent was blocked. That's a recipe for stale
    audit-row confusion (e.g. "the gate sees vacant but QSS shows
    alpha"). This endpoint collapses that into one JSON.

Endpoint:
    GET /api/admin/seat-registry/diagnose
        Returns:
          {
            "as_of": iso,
            "canonical_seats": [...],
            "roster_assignments": {...},
            "legacy_executor_seat_doc": {...},
            "gate_view": {
                seat: { holder, source, lane_scope, may_execute }
            },
            "drift": [
                { seat, roster_says, legacy_says, gate_sees }
            ],
            "lane_executor_summary": {
                "equity":  { holder, would_route_pass: bool, reason },
                "crypto":  { holder, would_route_pass: bool, reason }
            },
            "stale_block_warning": "..."
          }
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends

from auth import get_current_user
from db import db
from namespaces import SHARED_EXECUTOR_SEAT
from shared.executor_seat import get_seat_holder, seats_with_execute
from shared.roster import get_roster
from shared.seat_policy import CANONICAL_SEATS, SEAT_POLICY, seat_may_execute_lane


router = APIRouter(prefix="/admin/seat-registry", tags=["seat-registry-diagnose"])


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@router.get("/diagnose")
async def diagnose(_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    """Return the live seat-registry state as the execution gate sees it.

    Read-only. Never mutates. Safe to hit repeatedly.
    """
    # --- raw sources ---
    legacy_doc = await db[SHARED_EXECUTOR_SEAT].find_one(
        {"_id": "executor"}, {"_id": 0}
    ) or {}
    roster = await get_roster()
    assignments = (roster or {}).get("assignments") or {}

    # --- per-seat gate view ---
    gate_view: dict[str, Any] = {}
    drift: list[dict[str, Any]] = []
    for seat in CANONICAL_SEATS:
        holder = await get_seat_holder(seat)
        roster_says = assignments.get(seat)
        legacy_says = legacy_doc.get("holder") if seat == "executor" else None
        policy = SEAT_POLICY.get(seat) or {}
        gate_view[seat] = {
            "holder": holder,
            "source": (
                "legacy_executor_seat" if (seat == "executor" and legacy_says and holder == legacy_says)
                else "roster" if holder
                else "vacant"
            ),
            "lane_scope": policy.get("lane_scope"),
            "may_execute": bool(policy.get("may_execute")),
        }
        # Drift detection: does the legacy doc disagree with the roster?
        if seat == "executor" and legacy_says and roster_says and legacy_says != roster_says:
            drift.append({
                "seat": seat,
                "roster_says": roster_says,
                "legacy_says": legacy_says,
                "gate_sees": holder,
                "severity": "high",
                "fix": (
                    "Either POST /api/executor/rotate to align the legacy doc "
                    "with the roster, or wipe the legacy doc (set holder=None) "
                    "so the gate falls back to the roster."
                ),
            })

    # --- lane-executor summary: would a fresh intent route? ---
    lane_summary: dict[str, Any] = {}
    for lane in ("equity", "crypto"):
        eligible_seats = seats_with_execute(lane)
        chosen_holder = None
        chosen_seat = None
        for seat_name in eligible_seats:
            h = await get_seat_holder(seat_name)
            if h:
                chosen_holder = h
                chosen_seat = seat_name
                break
        if chosen_holder and seat_may_execute_lane(chosen_seat, lane):
            lane_summary[lane] = {
                "executor_seat": chosen_seat,
                "holder": chosen_holder,
                "would_route_pass": True,
                "reason": f"{chosen_holder} holds {chosen_seat} for lane={lane}",
            }
        else:
            lane_summary[lane] = {
                "executor_seat": chosen_seat,
                "holder": None,
                "would_route_pass": False,
                "reason": (
                    f"no brain holds an execute-capable seat for lane={lane}. "
                    f"Assign one via POST /api/admin/roster/assign."
                ),
            }

    return {
        "as_of": _now_iso(),
        "canonical_seats": list(CANONICAL_SEATS),
        "roster_assignments": {k: assignments.get(k) for k in CANONICAL_SEATS},
        "legacy_executor_seat_doc": {
            "holder": legacy_doc.get("holder"),
            "since": legacy_doc.get("since"),
            "assigned_by": legacy_doc.get("assigned_by"),
            "reason": legacy_doc.get("reason"),
        },
        "gate_view": gate_view,
        "drift": drift,
        "lane_executor_summary": lane_summary,
        "stale_block_warning": (
            "Intent gate_state values are STAMPED at evaluation time and are "
            "not re-run on display. If you see blocked intents whose 'failing "
            "gate' is executor_seat_check from before the current seat "
            "assignments existed, those are historical stamps — click "
            "dry-run on the row to re-evaluate against the live registry."
        ),
    }
