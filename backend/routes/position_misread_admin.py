"""Admin endpoints to surface the position-misread audit.

Read-only — no execution mutations. Backs the "last 20 misreads"
UI card the operator asked for as Priority 2 of the 2026-06-09
post-AAPL plan.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from auth import get_current_user
from shared.position_misread_audit import (
    list_recent_misreads, misread_summary_24h,
)
from db import db


# ── Quick-release enforcement toggle (2026-06-09) ───────────────
# Operator directive: the position-misread classifier ships in
# AUDIT-ONLY mode and never blocks a trade. When evidence
# accumulates and the operator wants to upgrade it to a real gate,
# they flip ONE Mongo doc via this endpoint — no restart, no
# redeploy. That's the "quick release" they asked for.
#
# Modes:
#   "audit_only"  — DEFAULT. Observer writes misread rows; no trade
#                   is blocked or modified. This is what's live.
#   "warn"        — Same as audit_only PLUS surface a banner in the
#                   UI on every misread (not implemented yet — flag
#                   accepted, behaviour identical to audit_only).
#   "block"       — Future gate: refuses to route any intent whose
#                   classifier flags MISREAD_POSITION_SIDE. NOT YET
#                   WIRED into the gate chain. When flipped to
#                   "block" today, behaviour is still audit_only —
#                   the gate code lands in a separate change.
#
# Flipping the toggle takes effect IMMEDIATELY on the next intent
# the auto-router picks. The runtime reads the toggle on every
# gate evaluation (fail-closed: read error = stay at audit_only,
# never escalate without explicit operator action).
ENFORCEMENT_COLL = "position_misread_config"
ENFORCEMENT_DOC_ID = "singleton"
DEFAULT_MODE = "audit_only"
VALID_MODES = ("audit_only", "warn", "block")


async def _get_enforcement_doc() -> dict:
    doc = await db[ENFORCEMENT_COLL].find_one(
        {"_id": ENFORCEMENT_DOC_ID}, {"_id": 0},
    )
    if not doc:
        return {
            "mode": DEFAULT_MODE,
            "updated_at": None,
            "updated_by": None,
            "reason": "default — first-boot, never explicitly set",
        }
    return doc


async def is_misread_enforcement_enabled() -> bool:
    """Single read the future gate code will use. Fail-closed: any
    read error or missing doc → returns False (= audit_only).
    Never escalates silently."""
    try:
        d = await _get_enforcement_doc()
        return d.get("mode") == "block"
    except Exception:  # noqa: BLE001
        return False


router = APIRouter(prefix="/admin/position-misreads", tags=["admin-misreads"])


@router.get("/recent")
async def position_misreads_recent(
    limit: int = Query(20, ge=1, le=200),
    _user: dict = Depends(get_current_user),
):
    """Last N position misreads, newest first. Use this in the UI to
    show the operator the brain-vs-broker disagreement stream.

    Each row carries everything needed to triage:
      * symbol, brain, lane, emitted_action
      * assumed_side (what brain thought)  vs  actual_side (broker)
      * correct_intent_type (OPEN/ADD/REDUCE/CLOSE/FLIP)
      * missed_short_profit (the AAPL-specific flag)
      * note (intent_id + setup_score backref)
    """
    items = await list_recent_misreads(db, limit)
    return {"items": items, "count": len(items)}


@router.get("/summary-24h")
async def position_misreads_summary_24h(
    _user: dict = Depends(get_current_user),
):
    """One-number heuristic the operator asked for:

        0-2  misreads/day  → AAPL was isolated
        20-50 misreads/day → systemic position-state problem

    Run this every morning. If `verdict` says
    `isolated_likely_aapl_only` for ~3 days, the AAPL incident was
    a one-off and the live execution path can stay as-is. If it
    says `systemic — ...`, time to wire the classifier into
    `_evaluate_gates` and start blocking on misread."""
    return await misread_summary_24h(db)


class EnforcementSet(BaseModel):
    """Quick-release toggle payload. `mode` MUST be one of the
    documented values — typos fail loudly at validation rather
    than silently escalating."""
    mode: str = Field(..., description="audit_only | warn | block")
    reason: str = Field("", max_length=500)


@router.get("/enforcement")
async def get_enforcement(_user: dict = Depends(get_current_user)):
    """Read the current position-misread enforcement mode.

    The runtime reads this on every gate evaluation, so changes
    take effect on the very next intent — no restart required.
    """
    return await _get_enforcement_doc()


@router.post("/enforcement")
async def set_enforcement(
    body: EnforcementSet,
    user: dict = Depends(get_current_user),  # noqa: B008
):
    """QUICK-RELEASE toggle. Flips the position-misread classifier
    between observer-only (`audit_only`) and active gate (`block`).

    Takes effect IMMEDIATELY on the next auto-router tick — no
    restart, no redeploy. Operator can race the trade engine: if
    a hiccup appears, flip back to `audit_only` and trading resumes
    unaffected. The current `block` mode is the operator-armed
    state — until then `audit_only` keeps experimentation safe.

    Audit trail: every flip is persisted with `updated_by`,
    `updated_at`, and `reason`. The default
    (no doc / first-boot) is `audit_only` — fail-closed.
    """
    if body.mode not in VALID_MODES:
        from fastapi import HTTPException
        raise HTTPException(
            status_code=400,
            detail=f"mode must be one of {VALID_MODES}, got {body.mode!r}",
        )
    from datetime import datetime, timezone
    actor = user.get("email") or "operator"
    doc = {
        "_id": ENFORCEMENT_DOC_ID,
        "mode": body.mode,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "updated_by": actor,
        "reason": body.reason or "(no reason given)",
    }
    await db[ENFORCEMENT_COLL].replace_one(
        {"_id": ENFORCEMENT_DOC_ID}, doc, upsert=True,
    )
    return {
        "ok": True,
        "mode": body.mode,
        "updated_by": actor,
        "takes_effect": "immediately on next gate evaluation",
        "rollback_command": (
            f"POST /api/admin/position-misreads/enforcement "
            f"{{\"mode\":\"audit_only\",\"reason\":\"rollback\"}}"
        ),
    }
