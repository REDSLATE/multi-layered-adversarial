"""Seat-state single chokepoint.

2026-02-20 — one source of truth for seat holders.

Doctrine: `brain_roster.assignments` is the operator-facing source of
truth for who holds each seat. The legacy `shared_executor_seat` and
`shared_auditor_seat` docs are deprecated. The Paradox v2 trust list
(`paradox_v2_seat_trusted_brains`) is a DERIVED collection: every
roster write mirrors the new holder into the trust list so the
unified pipeline (`shared.pipeline.seat_policy`) can authorize the
broker call without re-reading the roster.

Public API:
    mirror_executor_to_v2_trust(lane, brain_id)
        Called by `shared.roster.assign` after every successful write
        to the legacy roster. Sets the v2 trust list to {brain_id} for
        the matching executor seat. Removes other trusted brains for
        that seat (single-holder semantics — the operator's UI shows
        ONE brain per seat, not a list).

    migrate_legacy_auditor_to_roster()
        Idempotent. Copies `shared_auditor_seat.holder` into
        `brain_roster.assignments.auditor` if the latter is None. Runs
        at boot. Logs a one-line "migrated" or "no-op" message.

    cleanup_legacy_collections()
        Drops `shared_executor_seat` and `shared_auditor_seat`. Only
        callable from the `/api/admin/seat-state/cleanup-legacy`
        endpoint after the operator confirms display is clean.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from db import db
from namespaces import (
    BRAIN_ROSTER,
    PARADOX_V2_SEAT_TRUSTED,
    SHARED_AUDITOR_SEAT,
    SHARED_EXECUTOR_SEAT,
)


logger = logging.getLogger("seat_state")


# Map legacy roster role key → unified-pipeline executor seat_id.
# Only the two executor seats are mirrored — non-executor seats
# (strategist/governor/auditor) are display-only and not authorized
# to place orders, so they don't need a trust-list row.
_ROLE_TO_V2_SEAT: dict[str, str] = {
    "executor": "equity_executor",
    "crypto":   "crypto_executor",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def mirror_executor_to_v2_trust(role: str, brain_id: Optional[str]) -> None:
    """Make the v2 trust list reflect the legacy roster's executor
    assignment for this role. Single-holder semantics.

    If `brain_id` is None, the trust list for that seat is fully
    cleared — no one is trusted, no one can trade through that lane
    via the unified pipeline.
    """
    seat_id = _ROLE_TO_V2_SEAT.get(role)
    if not seat_id:
        return  # role isn't an executor; nothing to mirror.

    # Single-holder semantics: clear existing trust rows for this seat.
    await db[PARADOX_V2_SEAT_TRUSTED].delete_many({"seat_id": seat_id})

    if brain_id:
        await db[PARADOX_V2_SEAT_TRUSTED].insert_one({
            "seat_id": seat_id,
            "brain_id": brain_id,
            "trust_level": 1.0,
            "added_at": _now(),
            "added_by": "roster_assign_mirror",
        })
        logger.info("v2 trust mirrored: seat=%s brain=%s", seat_id, brain_id)
    else:
        logger.info("v2 trust mirrored: seat=%s cleared (no holder)", seat_id)


async def migrate_legacy_auditor_to_roster() -> dict:
    """One-shot, idempotent. If the legacy auditor doc has a holder
    AND the roster's `auditor` slot is empty, copy it across. Returns
    a small dict describing what happened — useful for boot logs."""
    audit = await db[SHARED_AUDITOR_SEAT].find_one(
        {"_id": "auditor"}, {"_id": 0, "holder": 1},
    ) or {}
    legacy_holder = audit.get("holder")
    if not legacy_holder:
        return {"migrated": False, "reason": "no_legacy_auditor_holder"}

    roster = await db[BRAIN_ROSTER].find_one({"_id": "current"}, {"_id": 0}) or {}
    assignments = roster.get("assignments") or {}
    if assignments.get("auditor"):
        return {"migrated": False, "reason": "roster_already_has_auditor"}

    await db[BRAIN_ROSTER].update_one(
        {"_id": "current"},
        {"$set": {
            "assignments.auditor": legacy_holder,
            "updated_at": _now(),
            "updated_by": "boot_migration_2026_02_20",
        }},
        upsert=True,
    )
    logger.info(
        "auditor migrated from legacy doc to roster: holder=%s",
        legacy_holder,
    )
    return {"migrated": True, "auditor_holder": legacy_holder}


async def sync_v2_trust_from_roster() -> dict:
    """Heal v2 trust list against the canonical roster on every boot.

    Idempotent. For each executor role in the roster, ensures the
    `paradox_v2_seat_trusted_brains` collection has exactly the
    operator's currently-assigned brain (or no rows when the slot is
    vacant). Picks up any drift introduced before the dual-write was
    wired in `roster.assign` — no operator clicks required.
    """
    roster = await db[BRAIN_ROSTER].find_one({"_id": "current"}, {"_id": 0}) or {}
    assignments = roster.get("assignments") or {}
    synced: dict[str, str | None] = {}
    for role_key in ("executor", "crypto"):
        brain = assignments.get(role_key)
        await mirror_executor_to_v2_trust(role_key, brain)
        synced[role_key] = brain
    logger.info("v2 trust sync from roster: %s", synced)
    return synced


async def cleanup_legacy_collections() -> dict:
    """Drop the two legacy seat collections. One-shot. Called from the
    admin cleanup endpoint after the operator confirms display is
    correct. Returns counts so the operator sees what was removed."""
    exec_count = await db[SHARED_EXECUTOR_SEAT].count_documents({})
    audit_count = await db[SHARED_AUDITOR_SEAT].count_documents({})
    await db[SHARED_EXECUTOR_SEAT].drop()
    await db[SHARED_AUDITOR_SEAT].drop()
    logger.warning(
        "legacy seat collections dropped: shared_executor_seat=%d shared_auditor_seat=%d",
        exec_count, audit_count,
    )
    return {
        "dropped": ["shared_executor_seat", "shared_auditor_seat"],
        "rows_removed": {
            "shared_executor_seat": exec_count,
            "shared_auditor_seat": audit_count,
        },
    }
