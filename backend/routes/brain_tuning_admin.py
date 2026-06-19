"""Admin endpoints for the brain-tuning override.

GET  /api/admin/brain-tuning            — current overrides + applied state
POST /api/admin/brain-tuning            — set per-lane overrides (partial OK)

Body shape (POST):
    {
      "equity": {"min_gap": 0.04, "min_confidence": 0.50},
      "crypto": {"min_gap": 0.025}
    }

Per-lane partial updates are allowed. Setting a field to `null`
removes that override (falls back to the brain's compiled default).
Setting an entire lane to `{}` removes ALL overrides for that lane.

Doctrine pin: this exposes brain-internal thresholds to the operator.
Lower `min_gap` → more directional intents (less HOLD). Lower
`min_confidence` → tier-1 confidence floor relaxed → more intents
pass Shelly. Use BOTH carefully — these are PRE-safety knobs; every
intent still goes through Seat/Governor/RoadGuard/Broker.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from auth import get_current_user
from db import db
from shared.brain_tuning_cache import refresh_cache


router = APIRouter(prefix="/admin/brain-tuning", tags=["brain-tuning"])

_FLAG_ID = "brain_tuning"
_ALLOWED_LANES = {"equity", "crypto"}
_ALLOWED_KNOBS = {"min_gap", "min_confidence", "hold_spread_coef"}

# Defensible bounds so the operator can't accidentally tank the brain
# math (e.g. min_gap=0 → every micro-flip becomes a trade).
_BOUNDS: dict[str, tuple[float, float]] = {
    "min_gap":          (0.005, 0.30),
    "min_confidence":   (0.30,  0.95),
    "hold_spread_coef": (0.0,   0.01),
}


class _LaneOverride(BaseModel):
    min_gap: Optional[float] = Field(default=None)
    min_confidence: Optional[float] = Field(default=None)
    hold_spread_coef: Optional[float] = Field(default=None)


class _SetBody(BaseModel):
    equity: Optional[_LaneOverride] = None
    crypto: Optional[_LaneOverride] = None


def _clamp(key: str, value: Optional[float]) -> Optional[float]:
    if value is None:
        return None
    lo, hi = _BOUNDS[key]
    return max(lo, min(hi, float(value)))


@router.get("")
async def get_state(_user: dict = Depends(get_current_user)) -> dict:
    doc = await db["runtime_flags"].find_one({"_id": _FLAG_ID}, {"_id": 0})
    return {
        "overrides": (doc or {}).get("overrides") or {},
        "updated_at": (doc or {}).get("updated_at"),
        "updated_by": (doc or {}).get("updated_by"),
        "bounds": {k: {"min": lo, "max": hi} for k, (lo, hi) in _BOUNDS.items()},
        "defaults_note": (
            "Compiled defaults (when override is unset): "
            "equity{min_gap=0.06, min_confidence=0.58, hold_spread_coef=0.002}, "
            "crypto{min_gap=0.03, min_confidence=0.58, hold_spread_coef=0.0008}."
        ),
    }


@router.post("")
async def set_state(
    body: _SetBody,
    user: dict = Depends(get_current_user),
) -> dict:
    # Build the clamped, validated overrides doc. Only include keys
    # the operator actually sent; null values remove the override.
    overrides: dict[str, dict] = {}
    for lane, payload in (("equity", body.equity), ("crypto", body.crypto)):
        if payload is None:
            continue
        lane_doc: dict[str, float] = {}
        for knob in _ALLOWED_KNOBS:
            val = _clamp(knob, getattr(payload, knob))
            if val is not None:
                lane_doc[knob] = val
        # Empty dict means "reset this lane to defaults" — write the
        # empty doc so the reader sees the lane key without any knobs.
        overrides[lane] = lane_doc

    # Merge with existing — don't wipe lanes the operator didn't send.
    existing = await db["runtime_flags"].find_one(
        {"_id": _FLAG_ID}, {"_id": 0, "overrides": 1},
    )
    merged = dict((existing or {}).get("overrides") or {})
    merged.update(overrides)

    await db["runtime_flags"].update_one(
        {"_id": _FLAG_ID},
        {"$set": {
            "_id": _FLAG_ID,
            "overrides": merged,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "updated_by": user.get("email") or "unknown",
        }},
        upsert=True,
    )
    # Force-refresh the in-process cache so the next intent sees it.
    await refresh_cache()
    return {"ok": True, "overrides": merged}
