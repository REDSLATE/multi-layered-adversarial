"""Position quorum computation — extracted from `shared/positions.py`.

2026-07-12 (P6a) extraction. Pure module: `_stance_summary` reads
from Mongo, `_compute_quorum` is a pure function of its inputs.
No behavior change from the prior in-file versions.

Doctrine (2026-05-30): a required seat is "engaged" iff the brain
CURRENTLY holding that seat has authored a stance on this
position. Authority lives in the SEAT; when the seat rotates, the
new holder must re-speak.
"""
from __future__ import annotations

from typing import Optional

from db import db
from namespaces import SHARED_POSITION_STANCES
from shared.seat_policy import required_seats


async def _stance_summary(position_id: str) -> dict:
    """Aggregate per-brain stance into a compact summary for list views."""
    rows = await db[SHARED_POSITION_STANCES].find(
        {"position_id": position_id}, {"_id": 0},
    ).sort("posted_at", 1).to_list(64)
    by_brain: dict[str, dict] = {}
    for r in rows:
        # Latest wins (a brain can refine its stance — last one stands).
        by_brain[r["brain"]] = r
    counts = {"long": 0, "short": 0, "abstain": 0}
    for stance in by_brain.values():
        if stance["stance"] in counts:
            counts[stance["stance"]] += 1
    return {
        "stances_by_brain": by_brain,
        "stance_counts": counts,
        "brains_engaged": len(by_brain),
    }


async def _compute_quorum(stances_by_brain: dict[str, dict],
                          stances_by_seat: dict[str, dict],
                          roster_assignments: dict[str, Optional[str]]) -> dict:
    """Quorum awareness — POSITION model (Doctrine, 2026-05-30).

    A required seat is "engaged" iff the brain CURRENTLY holding that
    seat has authored a stance on this position. Authority lives in
    the seat; when the seat rotates, the new holder must re-speak.
    A stance written by the previous holder no longer satisfies the
    seat's quorum — because authority moved with the seat.

    Prior implementation read `posted_as` (seat-at-write-time) and
    counted any historical stance under that seat as engagement,
    even after rotation. That allowed Alpha to take the strategist
    seat while Camaro's old strategist stance silently held quorum
    on his behalf — which is brain-coupling masquerading as
    "history". Same fix family as the executor_seat_check
    position-model relaxation (2026-05-28).

    Computes:
      - seats_engaged: required seats whose CURRENT holder has stanced
      - seats_required: list of seats marked seat_required=True
      - seats_missing: required seats whose current holder is silent
        (either no stance from current holder, or seat is vacant)
      - vacant_required_seats: required seats that have no brain assigned
        (worse than silent — there's literally no one to ask)
      - adversarial_blindness: auditor seat is required and unstamped
        (2026-05-27 — opponent merged into auditor; this flag now
        triggers on auditor silence)
      - governance_blindness: governor seat is required and unstamped
      - degraded: any required seat is unstamped or vacant
    """
    _ = stances_by_seat  # kept for signature compat; position-model
    # engagement is `current_holder in stances_by_brain`.
    req = list(required_seats())
    engaged: list[str] = []
    missing: list[str] = []
    vacant_required: list[str] = []
    for seat in req:
        current_holder = roster_assignments.get(seat)
        if not current_holder:
            vacant_required.append(seat)
            missing.append(seat)
            continue
        # Position-model engagement: current holder must have stanced.
        if current_holder in stances_by_brain:
            engaged.append(seat)
        else:
            missing.append(seat)
    return {
        "seats_engaged": engaged,
        "seats_required": req,
        "seats_missing": missing,
        "vacant_required_seats": vacant_required,
        # 2026-05-27 doctrine merge: opponent merged into auditor. The
        # auditor now carries BOTH pre-trade-contrary AND post-trade
        # review. Adversarial blindness now triggers when the auditor
        # is silent on a position.
        "adversarial_blindness": "auditor" in missing,
        "governance_blindness": "governor" in missing,
        "degraded": len(missing) > 0,
    }
