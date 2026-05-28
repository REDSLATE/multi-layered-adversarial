"""Opinion-silent watchdog (2026-05-28, pass #20).

Operator pattern from Alpha-author iter-106z11 follow-up:
  "Add an opinion-silent watchdog on the MC side that emits an
  `agent_activity` row when any occupied seat goes > threshold without
  an opinion POST. The Intents page already detects it (the ⚠
  counter); making it a logged event means it surfaces in alerts
  the moment a sidecar regression happens, instead of waiting for an
  operator to notice trades aren't firing."

Doctrine:
  This watchdog OBSERVES only. It NEVER:
    - Forces a seat reassignment
    - Vetoes an intent
    - Modifies execution authority
  It writes one row per silent seat per scan window to
  `opinion_silence_alerts`. Operator-facing alerts can poll that
  collection. The Seat Roster strip continues to be the live UI.

  Authority: ADVISORY observability only.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query

from auth import get_current_user
from db import db
from namespaces import (
    LIVE_RUNTIMES,
    SHARED_OPINIONS,
)


logger = logging.getLogger("risedual.opinion_silence_watchdog")
router = APIRouter(prefix="/admin", tags=["opinion-silence-watchdog"])


# Default silence threshold — same band the Seat Roster strip's
# orange chip uses (OPINION_FRESH_SEC * 4 = 4h).
DEFAULT_SILENCE_THRESHOLD_SEC = 4 * 60 * 60
# Per-(brain, seat) write throttle — don't spam a row every poll if
# the situation hasn't changed.
ALERT_COOLDOWN_SEC = 30 * 60


async def _last_opinion_age(brain: str) -> Optional[float]:
    """Seconds since brain's last opinion POST. None if never."""
    row = await db[SHARED_OPINIONS].find_one(
        {"runtime": brain},
        {"_id": 0, "created_at": 1},
        sort=[("created_at", -1)],
    )
    if not row or not row.get("created_at"):
        return None
    try:
        ts = datetime.fromisoformat(
            (row["created_at"] or "").replace("Z", "+00:00"),
        )
        return (datetime.now(timezone.utc) - ts).total_seconds()
    except (ValueError, TypeError):
        return None


async def _seat_assignments() -> dict[str, Optional[str]]:
    """Current seat assignments, joined from roster snapshot."""
    from shared.roster import get_roster
    snap = await get_roster()
    return (snap or {}).get("assignments") or {}


async def _recent_alert_exists(
    brain: str, seat: str, cooldown_sec: int,
) -> bool:
    """Was an alert for this (brain, seat) written within cooldown?"""
    cutoff = datetime.now(timezone.utc).timestamp() - cooldown_sec
    row = await db["opinion_silence_alerts"].find_one(
        {
            "brain": brain, "seat": seat,
            "ts_epoch": {"$gte": cutoff},
        },
        {"_id": 0},
    )
    return row is not None


@router.post("/opinion-silence-watchdog/scan")
async def scan(
    threshold_sec: int = Query(
        DEFAULT_SILENCE_THRESHOLD_SEC, ge=60, le=86400,
        description="seconds without opinion to consider 'silent'",
    ),
    cooldown_sec: int = Query(
        ALERT_COOLDOWN_SEC, ge=60, le=86400,
        description="don't re-alert same (brain,seat) within this window",
    ),
    dry_run: bool = Query(
        False, description="don't write alerts, just return what would be written",
    ),
    _user: dict = Depends(get_current_user),
):
    """Scan every occupied seat. For each holder whose last opinion is
    older than threshold OR has never posted: emit one row to
    `opinion_silence_alerts` (unless an alert for that (brain, seat)
    was written within cooldown).

    Doctrine: ADVISORY observability only. No execution authority is
    affected by this scan.
    """
    assignments = await _seat_assignments()
    now_iso = datetime.now(timezone.utc).isoformat()
    now_epoch = datetime.now(timezone.utc).timestamp()

    flagged: list[dict] = []
    skipped_cooldown: list[dict] = []
    skipped_fresh: list[dict] = []

    for seat, brain in assignments.items():
        if not brain or brain not in LIVE_RUNTIMES:
            continue
        age = await _last_opinion_age(brain)
        if age is not None and age <= threshold_sec:
            skipped_fresh.append({"seat": seat, "brain": brain, "age_sec": age})
            continue
        if not dry_run and await _recent_alert_exists(brain, seat, cooldown_sec):
            skipped_cooldown.append({"seat": seat, "brain": brain, "age_sec": age})
            continue
        alert = {
            "brain": brain,
            "seat": seat,
            "age_sec": age,
            "threshold_sec": threshold_sec,
            "kind": "never" if age is None else "stale",
            "ts": now_iso,
            "ts_epoch": now_epoch,
            "authority": "advisory_observability_only",
            "doctrine_note": (
                "Seat is occupied but the holder has not posted an opinion "
                "within threshold. MC has fallen back to deterministic "
                "doctrine sidecar for this brain's voice on every intent."
            ),
        }
        if not dry_run:
            await db["opinion_silence_alerts"].insert_one(alert)
            # Pop _id mongo just added in-place
            alert.pop("_id", None)
        flagged.append(alert)

    return {
        "ok": True,
        "ts": now_iso,
        "threshold_sec": threshold_sec,
        "cooldown_sec": cooldown_sec,
        "dry_run": dry_run,
        "flagged": flagged,
        "flagged_count": len(flagged),
        "skipped_cooldown": skipped_cooldown,
        "skipped_fresh": skipped_fresh,
        "occupied_seats_scanned": sum(
            1 for b in assignments.values() if b in LIVE_RUNTIMES
        ),
        "doctrine": "advisory_observability_only",
    }


@router.get("/opinion-silence-watchdog/recent")
async def recent(
    limit: int = Query(50, ge=1, le=500),
    _user: dict = Depends(get_current_user),
):
    """Last N alerts written by the watchdog. Operator-facing read."""
    rows = await db["opinion_silence_alerts"].find(
        {}, {"_id": 0},
    ).sort("ts_epoch", -1).to_list(limit)
    return {
        "items": rows,
        "count": len(rows),
        "doctrine": "advisory_observability_only",
    }
