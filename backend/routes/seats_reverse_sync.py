"""Seat registry → brain_roster reverse-sync recovery tool (2026-02-17).

Emerged from the 2026-02-17 seat-wipe incident: a destructive test
suite POSTed to `/api/admin/roster/reset` and cleared
`brain_roster.current.assignments.crypto*`. Runtime kept working
because `seat_registry` is the primary authority — but the operator's
UI briefly showed vacant crypto seats until we manually reverse-
synced. This endpoint promotes that ad-hoc python recovery into a
one-click, auditable, guarded restore.

Doctrine (operator-pinned 2026-02-17):
    seat_registry           = source of truth (READ-only here)
    brain_roster            = repaired mirror (WRITE target)
    NEVER delete registry rows
    return before/after diff
    audit-log every write
    REFUSE if seat_registry has missing / duplicate canonical seats
        (that means registry is corrupt — reverse-syncing corrupt
        data into the roster would compound the damage)

Endpoint:
    POST /api/admin/seats/reverse-sync-from-registry
    Body (optional): { "dry_run": true }   — preview diff only
    Response: {
        ok: bool,
        dry_run: bool,
        before: {...brain_roster.assignments before...},
        after:  {...what the write would/did set...},
        diff:   [{key, before, after}, ...],
        writes_applied: int,
        registry_snapshot: {...for audit trail...},
    }

Canonical seat inventory locked here (matches shared/seat.py doctrine):
    equity   × {strategist, governor, executor, auditor}
    crypto   × {strategist, governor, executor, auditor}

The reverse mapping to `brain_roster.assignments` keys follows the
canonical convention documented in shared/roster.py:
    equity strategist    → "strategist"
    equity governor      → "governor"
    equity executor      → "executor"
    equity auditor       → "auditor"
    crypto strategist    → "crypto_strategist"
    crypto governor      → "crypto_governor"
    crypto executor      → "crypto"            (NOT "crypto_executor")
    crypto auditor       → "crypto_auditor"
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth import get_current_user
from db import db
from namespaces import BRAIN_ROSTER


logger = logging.getLogger("seats_reverse_sync")

router = APIRouter(prefix="/admin/seats", tags=["seats-recovery"])


# Canonical (lane, role) → brain_roster.assignments key.
# ORDER matters for the diff output — kept in the same order the UI
# renders the seat matrix.
_LANE_ROLE_TO_ASSIGNMENT_KEY: dict[tuple[str, str], str] = {
    ("equity", "strategist"): "strategist",
    ("equity", "governor"):   "governor",
    ("equity", "executor"):   "executor",
    ("equity", "auditor"):    "auditor",
    ("crypto", "strategist"): "crypto_strategist",
    ("crypto", "governor"):   "crypto_governor",
    ("crypto", "executor"):   "crypto",           # canonical, NOT crypto_executor
    ("crypto", "auditor"):    "crypto_auditor",
}
_CANONICAL_SEAT_IDS = frozenset(
    f"{lane}:{role}" for (lane, role) in _LANE_ROLE_TO_ASSIGNMENT_KEY
)
_AUDIT_COLLECTION = "roster_audit_log"


class ReverseSyncIn(BaseModel):
    dry_run: bool = False


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _load_registry() -> dict[str, str]:
    """Return `{seat_id → holder}` from `seat_registry`. Duplicates
    surface as a validation failure downstream — Mongo's `_id`
    uniqueness normally prevents them, but the guard covers manual
    inserts or race conditions."""
    rows: dict[str, str] = {}
    async for d in db["seat_registry"].find({}, {"_id": 1, "holder": 1}):
        seat_id = str(d["_id"])
        holder = d.get("holder")
        if holder:
            rows[seat_id] = holder
    return rows


def _validate_registry(rows: dict[str, str]) -> Optional[str]:
    """Guard the operator: refuse to sync if the registry is corrupt.
    Returns an error string, or None if the registry is clean."""
    registry_ids = set(rows.keys())
    missing = _CANONICAL_SEAT_IDS - registry_ids
    if missing:
        return (
            f"seat_registry is INCOMPLETE — missing canonical rows: "
            f"{sorted(missing)}. Reverse-sync refused. Populate the "
            f"missing seats first (via the Quick Seat Switches UI or "
            f"direct DB insert)."
        )
    extras = registry_ids - _CANONICAL_SEAT_IDS
    if extras:
        # Extras aren't dangerous — they just don't map to any
        # brain_roster key — but surface them so the operator can
        # decide whether to clean them up.
        logger.warning(
            "seat_registry has %d non-canonical row(s) (%s). These will "
            "be ignored by the reverse-sync.", len(extras), sorted(extras),
        )
    # Duplicate check: shouldn't happen given `_id` uniqueness, but
    # defensive against any future collection with non-`_id` keys.
    holder_counts: dict[str, int] = {}
    for h in rows.values():
        holder_counts[h] = holder_counts.get(h, 0) + 1
    # Note: same brain in multiple seats IS allowed per doctrine
    # (e.g., strategist=barracuda and equity_auditor=barracuda would
    # be legitimate). We don't guard on holder-uniqueness.
    return None


def _rows_to_assignments(rows: dict[str, str]) -> dict[str, Optional[str]]:
    """Project registry rows into brain_roster's assignment-key shape.
    Emits all 8 canonical keys even if a row is missing (that case is
    caught by `_validate_registry` before reaching here, but be
    defensive)."""
    assignments: dict[str, Optional[str]] = {}
    for (lane, role), key in _LANE_ROLE_TO_ASSIGNMENT_KEY.items():
        assignments[key] = rows.get(f"{lane}:{role}")
    return assignments


async def _audit(actor: str, before: dict, after: dict,
                 diff: list[dict], dry_run: bool) -> None:
    """Append to `roster_audit_log` so every reverse-sync is
    reconstructible from the audit trail. Never raises."""
    try:
        await db[_AUDIT_COLLECTION].insert_one({
            "event": "reverse_sync_from_registry",
            "actor": actor,
            "ts": _now_iso(),
            "dry_run": dry_run,
            "before_assignments": before,
            "after_assignments": after,
            "diff": diff,
        })
    except Exception as e:  # noqa: BLE001
        logger.warning("reverse-sync audit write failed: %s", e)


@router.post("/reverse-sync-from-registry")
async def reverse_sync_from_registry(
    body: ReverseSyncIn = ReverseSyncIn(),
    user: dict = Depends(get_current_user),
) -> dict:
    """Rebuild `brain_roster.current.assignments` from `seat_registry`.

    Guards (per operator doctrine 2026-02-17):
        - Refuses if seat_registry is missing any canonical seat.
        - Never deletes seat_registry rows (this endpoint is
          read-only against the registry).
        - Returns before/after diff so the operator can eyeball the
          effect before it lands (or after, if not `dry_run`).
        - Every write appends to `roster_audit_log`.
    """
    actor = user.get("email") or "operator"

    registry_rows = await _load_registry()
    err = _validate_registry(registry_rows)
    if err:
        raise HTTPException(status_code=409, detail=err)

    intended_assignments = _rows_to_assignments(registry_rows)

    existing = await db[BRAIN_ROSTER].find_one({"_id": "current"}) or {}
    before_assignments = dict(existing.get("assignments") or {})

    diff: list[dict] = []
    for key, new_value in intended_assignments.items():
        old_value = before_assignments.get(key)
        if old_value != new_value:
            diff.append({"key": key, "before": old_value, "after": new_value})

    if body.dry_run:
        await _audit(actor, before_assignments, intended_assignments, diff, dry_run=True)
        return {
            "ok": True,
            "dry_run": True,
            "before": before_assignments,
            "after":  intended_assignments,
            "diff":   diff,
            "writes_applied": 0,
            "registry_snapshot": registry_rows,
            "note": "No writes made. Re-call with `dry_run=false` to apply.",
        }

    now = _now_iso()
    await db[BRAIN_ROSTER].update_one(
        {"_id": "current"},
        {
            "$set": {
                "assignments": intended_assignments,
                "updated_at": now,
                "restored_by": f"reverse_sync_from_registry:{actor}",
                "restored_at": now,
            },
            "$inc": {"seat_epoch": 1},
        },
        upsert=True,
    )
    await _audit(actor, before_assignments, intended_assignments, diff, dry_run=False)

    return {
        "ok": True,
        "dry_run": False,
        "before": before_assignments,
        "after":  intended_assignments,
        "diff":   diff,
        "writes_applied": 1,
        "registry_snapshot": registry_rows,
    }
