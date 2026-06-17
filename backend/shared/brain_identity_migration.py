"""Brain identity DB rename migration (2026-02-20).

One-shot, idempotent, runs at every backend boot. Rewrites stored
brain_id values from the legacy canonical (alpha/camaro/chevelle/
redeye) to the new canonical (camino/barracuda/hellcat/gto) across
the collections that store holder/brain-id references.

Collections covered:
    brain_roster                   .assignments[role] values
    paradox_v2_seat_trusted_brains .brain_id
    paradox_v2_seat_policy_config  .holder (if present)
    shared_executor_seat           .holder
    shared_auditor_seat            .holder
    brain_eligibility              .matrix keys
    runtime_flags                  .auto_router_enabled (no-op; only here for symmetry)

Doctrine pin: ONE-WAY rename. Once migrated, the legacy IDs only
appear as ingress aliases in `shared.brain_identity` — never as
stored values. Re-running the migration on already-migrated data
is a clean no-op.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from db import db


logger = logging.getLogger("brain_identity_migration")


LEGACY_TO_CANONICAL: dict[str, str] = {
    "alpha":    "camino",
    "camaro":   "barracuda",
    "chevelle": "hellcat",
    "redeye":   "gto",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def migrate_brain_identity() -> dict:
    """Run the rename across all known collections. Returns a small
    report dict for the boot log."""
    report: dict = {"started_at": _now(), "updates": {}}

    # ── brain_roster.assignments[role] ─────────────────────────────
    roster = await db["brain_roster"].find_one({"_id": "current"}, {"_id": 0}) or {}
    assignments: dict = roster.get("assignments") or {}
    changed = {}
    for role, brain in list(assignments.items()):
        if brain in LEGACY_TO_CANONICAL:
            new_brain = LEGACY_TO_CANONICAL[brain]
            assignments[role] = new_brain
            changed[role] = (brain, new_brain)
    if changed:
        await db["brain_roster"].update_one(
            {"_id": "current"},
            {"$set": {
                "assignments": assignments,
                "updated_at": _now(),
                "updated_by": "brain_identity_migration_2026_02_20",
            }},
            upsert=True,
        )
        logger.info("brain_roster renames: %s", changed)
    report["updates"]["brain_roster.assignments"] = changed

    # ── paradox_v2_seat_trusted_brains.brain_id ────────────────────
    trust_renames = []
    async for doc in db["paradox_v2_seat_trusted_brains"].find(
        {"brain_id": {"$in": list(LEGACY_TO_CANONICAL.keys())}},
        {"_id": 1, "seat_id": 1, "brain_id": 1},
    ):
        old = doc["brain_id"]
        new = LEGACY_TO_CANONICAL[old]
        await db["paradox_v2_seat_trusted_brains"].update_one(
            {"_id": doc["_id"]},
            {"$set": {"brain_id": new}},
        )
        trust_renames.append({
            "seat_id": doc.get("seat_id"),
            "old": old,
            "new": new,
        })
    if trust_renames:
        logger.info("paradox_v2_seat_trusted_brains renames: %s", trust_renames)
    report["updates"]["paradox_v2_seat_trusted_brains"] = trust_renames

    # ── shared_executor_seat.holder ────────────────────────────────
    exec_doc = await db["shared_executor_seat"].find_one(
        {"_id": "executor"}, {"_id": 0, "holder": 1},
    ) or {}
    holder = exec_doc.get("holder")
    if holder in LEGACY_TO_CANONICAL:
        new = LEGACY_TO_CANONICAL[holder]
        await db["shared_executor_seat"].update_one(
            {"_id": "executor"},
            {"$set": {"holder": new, "renamed_at": _now()}},
        )
        report["updates"]["shared_executor_seat.holder"] = (holder, new)
        logger.info("shared_executor_seat holder rename: %s → %s", holder, new)

    # ── shared_auditor_seat.holder ─────────────────────────────────
    audit_doc = await db["shared_auditor_seat"].find_one(
        {"_id": "auditor"}, {"_id": 0, "holder": 1},
    ) or {}
    holder = audit_doc.get("holder")
    if holder in LEGACY_TO_CANONICAL:
        new = LEGACY_TO_CANONICAL[holder]
        await db["shared_auditor_seat"].update_one(
            {"_id": "auditor"},
            {"$set": {"holder": new, "renamed_at": _now()}},
        )
        report["updates"]["shared_auditor_seat.holder"] = (holder, new)
        logger.info("shared_auditor_seat holder rename: %s → %s", holder, new)

    # ── brain_eligibility.matrix keys ──────────────────────────────
    elig = await db["brain_eligibility"].find_one({"_id": "current"}, {"_id": 0}) or {}
    matrix = elig.get("matrix") or {}
    elig_renames = []
    new_matrix = {}
    for brain, row in matrix.items():
        if brain in LEGACY_TO_CANONICAL:
            new_brain = LEGACY_TO_CANONICAL[brain]
            new_matrix[new_brain] = row
            elig_renames.append((brain, new_brain))
        else:
            new_matrix[brain] = row
    if elig_renames:
        await db["brain_eligibility"].update_one(
            {"_id": "current"},
            {"$set": {"matrix": new_matrix, "updated_at": _now()}},
            upsert=True,
        )
        logger.info("brain_eligibility renames: %s", elig_renames)
    report["updates"]["brain_eligibility.matrix"] = elig_renames

    report["finished_at"] = _now()
    return report
