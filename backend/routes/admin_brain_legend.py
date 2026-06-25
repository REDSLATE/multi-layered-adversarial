"""Operator-visible legend for brain identity (2026-02-23 dual-field
migration). Surfaces the legacy→canonical translation table so anyone
reading historical "camaro" / "alpha" / "chevelle" / "redeye" audit
rows can resolve them to the canonical brain_id without grepping the
codebase.

Endpoint:
    GET /api/admin/brain-legend → list of legend docs (read-only)

The collection is seeded at boot in `lifespan.py` via
`seed_brain_legend()` — this route is just the read view.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from auth import get_current_user
from db import db
from shared.brain_legend import get_brain_legend


router = APIRouter(tags=["admin"])


@router.get("/admin/brain-legend")
async def admin_brain_legend(
    user: dict = Depends(get_current_user),  # noqa: B008, ARG001
):
    """Return the operator-facing brain identity legend.

    Shape:
        {
            "ok": true,
            "legend": [
                {
                    "canonical": "barracuda",
                    "display_name": "Barracuda",
                    "legacy_aliases": ["camaro"],
                    "doctrine_role": "...",
                    "migrated_at": "2026-02-23T...",
                    "migration_reason": "...",
                },
                ...
            ],
            "doctrine_note": (
                "stack_canonical is the AUTHORITATIVE identity field. "
                "stack is preserved verbatim as historical metadata."
            ),
        }
    """
    rows = await get_brain_legend(db)
    return {
        "ok": True,
        "legend": rows,
        "doctrine_note": (
            "stack_canonical is the AUTHORITATIVE identity field — "
            "all dashboards, gates and post-mortems read from it. "
            "stack is preserved verbatim as historical metadata so "
            "operators can trace any old doc back to its original "
            "wire form."
        ),
    }
