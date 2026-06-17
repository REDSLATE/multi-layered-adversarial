"""Operator-controlled broker-lane on/off toggles.

Doctrine pin (2026-02-XX):
    Each trading lane (`equity`, `crypto`) can be flipped on/off by
    the operator from production at runtime — NO redeploy required.
    The broker router consults `is_lane_enabled(lane)` BEFORE any
    credential lookup or broker call. A disabled lane returns
    NO_TRADE immediately.

    This is ORTHOGONAL to:
      - The static `LANE_BROKER_REGISTRY` in `broker_symbol_resolver`:
        which broker each lane routes to (Webull / Kraken). Decides
        identity, not whether to trade. (Prior to 2026-02-20 there
        was an env var `RISEDUAL_EQUITY_BROKER` for this; that path
        is dead — equity ALWAYS routes to Webull now.)
      - Webull/Kraken `execution_enabled` flags: per-broker kill
        switches. Decide whether THAT broker may fill orders.
      - Learning-ladder stage: per-brain-per-lane. Decides whether
        a SPECIFIC brain may trade THAT lane.

    The lane toggle is the COARSEST gate — operator says "kill
    equity entirely right now" → all 4 brains' equity intents
    NO_TRADE, regardless of ladder stage or credentials.

Defaults:
    Lanes default to ENABLED when no row exists. We NEVER silently
    shut a lane down by code change — explicit operator action is
    required to disable.

Endpoints:
    GET  /api/admin/broker/lanes              — current toggle states
    POST /api/admin/broker/lanes/{lane}/toggle — flip with audit
    GET  /api/admin/broker/lanes/audit         — last N toggle events
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Path
from pydantic import BaseModel, Field

from auth import get_current_user
from db import db
from namespaces import BROKER_LANE_AUDIT_LOG, BROKER_LANE_TOGGLES


logger = logging.getLogger("risedual.broker_lane")
router = APIRouter(prefix="/admin/broker/lanes", tags=["broker-lanes"])

# The only legitimate lane identifiers. Keeps a typo from accidentally
# creating a ghost `lane=equiti` toggle that does nothing.
KNOWN_LANES: tuple[str, ...] = ("equity", "crypto")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ──────────────────────── consumer-facing helper ────────────────────────


async def is_lane_enabled(lane: str) -> bool:
    """Return True iff this lane is currently allowed to trade.

    Default: TRUE (when no row exists). The operator must explicitly
    disable a lane to turn it off — fail-OPEN by design here, because
    every lane is downstream-gated by ladder/credentials/execution
    flags. The lane toggle is the OPERATOR'S override knob, not a
    safety default.
    """
    lane = (lane or "").strip().lower()
    if lane not in KNOWN_LANES:
        return False
    doc = await db[BROKER_LANE_TOGGLES].find_one(
        {"_id": lane}, {"enabled": 1},
    )
    if not doc:
        return True
    return bool(doc.get("enabled", True))


# ──────────────────────── admin endpoints ────────────────────────


class LaneToggleIn(BaseModel):
    enabled: bool
    confirm: str = Field(
        ...,
        description=(
            "Confirmation phrase. To enable:  'I authorize <lane> trading'  "
            "To disable: 'Disable <lane> trading'"
        ),
    )


@router.get("")
async def list_lane_toggles(_user: dict = Depends(get_current_user)):
    """Snapshot of every known lane's enabled state."""
    rows: list[dict] = []
    for lane in KNOWN_LANES:
        doc = await db[BROKER_LANE_TOGGLES].find_one(
            {"_id": lane}, {"_id": 0},
        )
        rows.append({
            "lane": lane,
            "enabled": True if not doc else bool(doc.get("enabled", True)),
            "updated_at": (doc or {}).get("updated_at"),
            "updated_by": (doc or {}).get("updated_by"),
            "doctrine": (
                "Lane toggle is the coarsest gate — disabled = NO_TRADE "
                "for the whole lane regardless of broker credentials or "
                "per-brain ladder stage. Defaults to enabled."
            ),
        })
    return {"items": rows, "count": len(rows)}


@router.post("/{lane}/toggle")
async def toggle_lane(
    body: LaneToggleIn,
    lane: str = Path(..., description="`equity` or `crypto`"),
    user: dict = Depends(get_current_user),
):
    """Flip a lane's enabled state. Audit-logged.

    Confirmation phrase prevents accidental flips:
        enable:  "I authorize equity trading"
        disable: "Disable equity trading"
    """
    lane = (lane or "").strip().lower()
    if lane not in KNOWN_LANES:
        raise HTTPException(
            status_code=404, detail=f"unknown lane {lane!r} — expected one of {KNOWN_LANES}",
        )

    expected_phrase = (
        f"I authorize {lane} trading" if body.enabled
        else f"Disable {lane} trading"
    )
    if body.confirm != expected_phrase:
        raise HTTPException(
            status_code=400,
            detail=f"confirmation phrase mismatch — expected: {expected_phrase!r}",
        )

    operator = (user or {}).get("email") or "operator"
    now = _now_iso()

    # Capture prior state for the audit row so we have before/after.
    prior = await db[BROKER_LANE_TOGGLES].find_one(
        {"_id": lane}, {"enabled": 1},
    )
    prior_enabled = True if not prior else bool(prior.get("enabled", True))

    await db[BROKER_LANE_TOGGLES].update_one(
        {"_id": lane},
        {
            "$set": {
                "enabled": body.enabled,
                "updated_at": now,
                "updated_by": operator,
            },
        },
        upsert=True,
    )
    await db[BROKER_LANE_AUDIT_LOG].insert_one({
        "ts": now,
        "lane": lane,
        "actor": operator,
        "prior_enabled": prior_enabled,
        "new_enabled": body.enabled,
        "confirm": body.confirm,
    })
    logger.info(
        "broker lane toggle lane=%s actor=%s %s→%s",
        lane, operator, prior_enabled, body.enabled,
    )
    return {
        "lane": lane,
        "enabled": body.enabled,
        "prior_enabled": prior_enabled,
        "updated_at": now,
        "updated_by": operator,
    }


@router.get("/audit")
async def lane_audit_log(
    limit: int = 50,
    lane: Optional[str] = None,
    _user: dict = Depends(get_current_user),
):
    """Read-only audit log of toggle events. Filterable by lane."""
    q: dict = {}
    if lane:
        q["lane"] = lane.strip().lower()
    rows = await db[BROKER_LANE_AUDIT_LOG].find(q, {"_id": 0}).sort(
        "ts", -1,
    ).to_list(min(max(limit, 1), 500))
    return {"items": rows, "count": len(rows)}
