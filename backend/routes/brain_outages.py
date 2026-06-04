"""Brain-outage history endpoint (2026-02-20).

Doctrine:
  MC already records every sidecar check-in in `sidecar_checkin_audit`
  with a precise timestamp. Outages are derivable from that audit
  log: any gap > a threshold between two consecutive check-ins
  for a brain IS an outage. No new collection, no new writes —
  purely a read over data we already keep.

  This module exposes:
    GET /api/admin/brain-outages?hours=24&min_gap_sec=300

  Each row in the response is one outage event with:
    brain, started_at (last check-in before the silence),
    ended_at (first check-in after, or `null` if currently down),
    duration_sec, recovered (bool).

  Operators use this to spot patterns ("Chevelle dies every ~2h →
  resource limit") that screenshot-archeology can't see.

Doctrine guardrail — ADVISORY OBSERVABILITY ONLY:
  * Reads sidecar_checkin_audit only
  * Never writes
  * Never affects authority, seats, or gates
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, Query

from auth import get_current_user
from db import db
from namespaces import DISCUSSION_PARTICIPANTS


router = APIRouter(prefix="/admin", tags=["brain-outages"])


# A gap >= this between consecutive check-ins counts as an outage.
# 300s (5 min) matches the DEAD heartbeat threshold so the operator's
# mental model is consistent ("DEAD on the dashboard = outage in the
# history").
DEFAULT_MIN_GAP_SEC = 300


def _parse_iso(s: str) -> datetime:
    return datetime.fromisoformat((s or "").replace("Z", "+00:00"))


async def _outages_for_brain(
    brain: str, since: datetime, min_gap_sec: int,
) -> List[Dict[str, Any]]:
    """Walk the brain's check-in audit log in chronological order
    and emit one outage per gap >= min_gap_sec."""
    cursor = db["sidecar_checkin_audit"].find(
        {"runtime": brain, "ts": {"$gte": since.isoformat()}},
        {"_id": 0, "ts": 1},
    ).sort("ts", 1)

    outages: List[Dict[str, Any]] = []
    prev_ts_str: str | None = None
    prev_dt: datetime | None = None

    async for row in cursor:
        cur_str = row.get("ts")
        try:
            cur_dt = _parse_iso(cur_str)
        except Exception:  # noqa: BLE001
            continue

        if prev_dt is not None:
            gap = (cur_dt - prev_dt).total_seconds()
            if gap >= min_gap_sec:
                outages.append({
                    "brain": brain,
                    "started_at": prev_ts_str,
                    "ended_at": cur_str,
                    "duration_sec": int(gap),
                    "recovered": True,
                })
        prev_ts_str = cur_str
        prev_dt = cur_dt

    # If the most recent check-in is itself > min_gap_sec old, the
    # brain is CURRENTLY down. Emit an open-ended outage row.
    if prev_dt is not None:
        now = datetime.now(timezone.utc)
        age = (now - prev_dt).total_seconds()
        if age >= min_gap_sec:
            outages.append({
                "brain": brain,
                "started_at": prev_ts_str,
                "ended_at": None,
                "duration_sec": int(age),
                "recovered": False,
            })

    return outages


@router.get("/brain-outages")
async def list_brain_outages(
    hours: int = Query(
        default=24, ge=1, le=24 * 14,
        description="Look-back window in hours. Default 24.",
    ),
    min_gap_sec: int = Query(
        default=DEFAULT_MIN_GAP_SEC, ge=60, le=24 * 3600,
        description=(
            "Gaps >= this between consecutive check-ins count as an "
            "outage. Default 300s matches the DEAD heartbeat band."
        ),
    ),
    _user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """Return every brain outage in the look-back window, derived
    from `sidecar_checkin_audit` gaps.

    Response:
        {
          "window_hours": 24,
          "min_gap_sec": 300,
          "per_brain": {
            "chevelle": {
              "outage_count": 3,
              "total_outage_sec": 12_960,
              "longest_outage_sec": 7_824,
              "currently_down": true,
              "events": [
                {"brain":"chevelle","started_at":"...","ended_at":"...",
                 "duration_sec":3_700,"recovered":true},
                ...
              ]
            },
            ...
          },
          "fleet_summary": {
            "total_outages": 7,
            "brains_currently_down": ["chevelle", "redeye"],
          },
          "doctrine": "advisory_observability_only",
        }
    """
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=hours)

    per_brain: Dict[str, Any] = {}
    brains_currently_down: List[str] = []
    fleet_total = 0

    for brain in DISCUSSION_PARTICIPANTS:
        events = await _outages_for_brain(brain, since, min_gap_sec)
        total = sum(e["duration_sec"] for e in events)
        longest = max((e["duration_sec"] for e in events), default=0)
        currently_down = bool(events and not events[-1]["recovered"])
        if currently_down:
            brains_currently_down.append(brain)
        per_brain[brain] = {
            "outage_count": len(events),
            "total_outage_sec": total,
            "longest_outage_sec": longest,
            "currently_down": currently_down,
            "events": events,
        }
        fleet_total += len(events)

    return {
        "window_hours": hours,
        "min_gap_sec": min_gap_sec,
        "per_brain": per_brain,
        "fleet_summary": {
            "total_outages": fleet_total,
            "brains_currently_down": sorted(brains_currently_down),
        },
        "doctrine": "advisory_observability_only",
        "computed_at": now.isoformat(),
    }
