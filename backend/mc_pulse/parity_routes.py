"""DEPRECATED — `parity_routes` is retired.

2026-07-12 (P4): the "parity" concept was runner-vs-pulse
comparison, which stopped being meaningful the moment the
runners were deleted (P3 step 3). Metrics that only made sense
with a runner denominator (`match_score`, `runner_count`,
`timestamp_drift`, `pairs_matched`, `rationale_jaccard_mean`,
`arbiter_flip_gates_pass`) have been removed. What remains lives
in `mc_pulse.pulse_health_routes` under a clearer name.

This file remains ONLY as a thin backward-compat alias so any
external caller still hitting `/api/mc/parity/{brain}` gets a
redirected response (via the pulse_health computation) for one
iteration. Delete this file entirely once the frontend has
migrated to `/api/mc/pulse-health/{brain}`.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query

from auth import get_current_user

from mc_pulse.pulse_health_routes import (
    compute_pulse_health,
    take_pulse_health_snapshot,  # re-export for lifespan BC
)

logger = logging.getLogger("mc_pulse.parity_deprecated")

router = APIRouter(prefix="/mc/parity", tags=["mc-parity-deprecated"])


@router.get("/{brain_id}")
async def parity_report_deprecated(
    brain_id: str,
    hours: int = Query(24, ge=1, le=168),
    sample_size: int = Query(20, ge=0, le=100),
    user: dict = Depends(get_current_user),
):
    """DEPRECATED — redirects to `compute_pulse_health`. `sample_size`
    ignored (samples are meaningless without a runner tape)."""
    logger.warning(
        "DEPRECATED /api/mc/parity/%s hit — client should migrate to "
        "/api/mc/pulse-health/%s", brain_id, brain_id,
    )
    return await compute_pulse_health(brain_id, hours=hours)


# Re-export names lifespan.py imported from this module during P1/P2.
PARITY_SNAPSHOT_BRAINS = ["camino", "gto", "barracuda", "hellcat"]
take_parity_snapshot = take_pulse_health_snapshot  # BC alias
