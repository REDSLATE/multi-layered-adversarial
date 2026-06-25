"""Brain identity legend — the single source of truth for legacy
stack codes → canonical brain_id translation.

Doctrine (2026-02-23 dual-field migration):

  • `stack` field on intent documents is the HISTORICAL identity
    label — preserved exactly as the brain emitted it. Never
    overwritten by MC. Operators can trace any old doc back to
    its original wire form via this field.

  • `stack_canonical` field is the AUTHORITATIVE identity key —
    always canonical brain_id (camino | barracuda | hellcat | gto),
    stamped at emission time and backfilled on existing docs by
    `scripts/migrate_stack_canonical.py`. ALL downstream code
    (gates, dashboards, post-mortem, auto-submit) MUST read from
    this field, never from `stack`.

  • `brain_legend` Mongo collection is the operator-visible legend.
    A read-only registry of the four canonical brains plus the
    legacy aliases that resolve to them. Surfaced in `/api/admin/
    brain-legend` and embedded in the Diagnostics page so anyone
    reading a 6-month-old "camaro" audit row knows what it means
    without grepping the codebase.

The shape of the legend doc is small and stable:

    {
        "_id": "barracuda",
        "canonical": "barracuda",
        "display_name": "Barracuda",
        "legacy_aliases": ["camaro"],
        "doctrine_role": "crypto_executor",
        "migrated_at": "2026-02-23T00:00:00+00:00",
        "migration_reason": "dual-field migration 2026-02-23 — "
                            "preserve historical stack labels, "
                            "route all reads via stack_canonical",
    }

Future renames (e.g. when "Hellcat" gets retired or a new brain
joins) edit this collection and the seeder via `seed_brain_legend()`
— no codebase grep required. The constants in `brain_doctrine.py`
remain the build-time fallback so cold boots before the first
seed still resolve correctly.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional


# ── Canonical brain_id set ───────────────────────────────────────────
# Mirrors `RUNTIMES` in namespaces.py / brain_doctrine.py. Defined
# locally so this module has no cyclic import path.
CANONICAL_BRAINS: frozenset[str] = frozenset({
    "camino", "barracuda", "hellcat", "gto",
})

# Legacy stack code → canonical brain_id. Frozen — additions must
# also seed an entry in `seed_brain_legend()`.
LEGACY_TO_CANONICAL: dict[str, str] = {
    "alpha":    "camino",
    "camaro":   "barracuda",
    "chevelle": "hellcat",
    "redeye":   "gto",
}

# Display name → canonical brain_id (case-insensitive). The
# canonical brain_id itself is the display name with a capital first
# letter, so we resolve them with `.lower()` at read time.
DISPLAY_NAMES: dict[str, str] = {
    "camino":    "Camino",
    "barracuda": "Barracuda",
    "hellcat":   "Hellcat",
    "gto":       "GTO",
}

# Display name → canonical brain_id (case-insensitive). The
# canonical brain_id is the lowercased display name, so
# `_normalize_brain_to_stack` resolves these with `.lower()` at
# read time.
DISPLAY_NAME_TO_CANONICAL: dict[str, str] = {
    name: canonical for canonical, name in DISPLAY_NAMES.items()
}

# Doctrine roles per canonical brain. Read from `roster.py` at
# runtime if you need the live seat assignment — the role label
# here is the brain's classical archetype for the legend's
# "what does this brain do" hover text.
DOCTRINE_ROLE: dict[str, str] = {
    "camino":    "equity_executor / crypto_auditor (classical)",
    "barracuda": "equity_strategist / crypto_executor (classical)",
    "hellcat":   "equity_governor / crypto_strategist (classical)",
    "gto":       "equity_auditor / crypto_governor (classical)",
}


def canonicalize_stack(raw: Optional[str]) -> str:
    """Resolve any brain identifier to its canonical brain_id.

    Pure function — no I/O, no DB lookup. Use this at write time
    so `stack_canonical` is correctly stamped without depending on
    the `brain_legend` collection being seeded.

    Returns the lowercased input unchanged if it can't be resolved
    (defensive: an unknown identifier surfaces in audit logs rather
    than getting silently rewritten).
    """
    if not raw:
        return ""
    key = raw.strip().lower()
    if not key:
        return ""
    if key in CANONICAL_BRAINS:
        return key
    if key in LEGACY_TO_CANONICAL:
        return LEGACY_TO_CANONICAL[key]
    # Display-name path — already lowercased above, so this catches
    # only the title-cased UI form coming through case-insensitively.
    if raw.capitalize() in DISPLAY_NAME_TO_CANONICAL:
        return DISPLAY_NAME_TO_CANONICAL[raw.capitalize()]
    return key


# ── Mongo seed ───────────────────────────────────────────────────────
BRAIN_LEGEND_COLLECTION = "brain_legend"


def _legend_doc(canonical: str) -> dict:
    """Build the legend document for a single canonical brain."""
    legacy_aliases = sorted(
        legacy for legacy, can in LEGACY_TO_CANONICAL.items()
        if can == canonical
    )
    return {
        "_id": canonical,
        "canonical": canonical,
        "display_name": DISPLAY_NAMES[canonical],
        "legacy_aliases": legacy_aliases,
        "doctrine_role": DOCTRINE_ROLE.get(canonical, ""),
        "migrated_at": datetime.now(timezone.utc).isoformat(),
        "migration_reason": (
            "dual-field migration 2026-02-23 — preserve historical "
            "stack labels, route all reads via stack_canonical"
        ),
    }


async def seed_brain_legend(db) -> dict:
    """Idempotent seed of the `brain_legend` collection.

    Safe to call on every cold boot (uses `replace_one(upsert=True)`).
    Returns a summary the operator can log: `{"seeded": [...],
    "total": N}`.
    """
    seeded: list[str] = []
    for canonical in sorted(CANONICAL_BRAINS):
        doc = _legend_doc(canonical)
        await db[BRAIN_LEGEND_COLLECTION].replace_one(
            {"_id": canonical}, doc, upsert=True,
        )
        seeded.append(canonical)
    return {"seeded": seeded, "total": len(seeded)}


async def get_brain_legend(db) -> list[dict]:
    """Read the full legend (operator-facing GET). Returns canonical-
    sorted list so the UI can render a deterministic table."""
    cursor = db[BRAIN_LEGEND_COLLECTION].find({}).sort("_id", 1)
    out: list[dict] = []
    async for doc in cursor:
        # Drop the `_id` since `canonical` is the operator-facing key.
        doc.pop("_id", None)
        out.append(doc)
    return out
