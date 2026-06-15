"""Admin route — auto-submit policy toggle + status (2026-02-19).

Phase 1 of the throughput unlock. Operator can flip the
`tier_1_conservative` policy on/off without redeploying. The policy
respects EVERY gate (it just auto-clicks SUBMIT on intents that
already passed dry-run and meet the conservative checklist).
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from auth import get_current_user
from db import db
from shared.auto_submit_policy import (
    TIER_1_DEFAULTS,
    get_policy,
    hydrate_from_mongo,
    set_policy_async,
)


router = APIRouter(prefix="/admin/auto-submit", tags=["admin-auto-submit"])


POLICY_AUDIT = "shared_auto_submit_policy_audit"


class PolicyBody(BaseModel):
    enabled: bool
    confidence_min: float | None = Field(default=None, ge=0.0, le=1.0)
    notional_default_usd: float | None = Field(default=None, gt=0.0)
    reason: str = Field(default="", max_length=400)


@router.get("/policy")
async def policy_status(_user: dict = Depends(get_current_user)) -> dict:
    """Current effective policy + defaults snapshot.

    Lazy-hydrates from Mongo on first access if the lifespan hook
    hasn't already (safety net for fork pods / scripts)."""
    from shared.auto_submit_policy import _HYDRATED
    if not _HYDRATED:
        await hydrate_from_mongo()
    return {
        "policy": get_policy(),
        "defaults": TIER_1_DEFAULTS,
    }


@router.post("/policy")
async def policy_toggle(
    body: PolicyBody,
    user: dict = Depends(get_current_user),
) -> dict:
    if body.enabled and len(body.reason.strip()) < 4:
        raise HTTPException(
            status_code=400,
            detail=(
                "enabling auto-submit requires a `reason` of ≥4 characters "
                "(audit-trail requirement)"
            ),
        )
    overrides = {}
    if body.confidence_min is not None:
        overrides["confidence_min"] = body.confidence_min
    if body.notional_default_usd is not None:
        overrides["notional_default_usd"] = body.notional_default_usd
    # set_policy_async PERSISTS the override to Mongo — this is the
    # fix for the 2026-02-19 incident where the toggle was wiped on
    # every K8s pod restart because we only had an in-memory dict.
    policy = await set_policy_async(enabled=body.enabled, **overrides)
    await db[POLICY_AUDIT].insert_one({
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": "toggle_enabled" if body.enabled else "toggle_disabled",
        "by": user.get("email"),
        "user_email": user.get("email"),
        "enabled": body.enabled,
        "reason": body.reason.strip(),
        "overrides": overrides,
        "persisted": True,
    })
    return {"ok": True, "policy": policy}


@router.get("/audit")
async def policy_audit(
    _user: dict = Depends(get_current_user),
    limit: int = 50,
) -> dict:
    limit = max(1, min(int(limit), 200))
    rows = await db[POLICY_AUDIT].find({}, {"_id": 0}).sort("ts", -1).to_list(length=limit)
    return {"audit": rows, "count": len(rows)}


@router.get("/recent-auto-trades")
async def recent_auto_trades(
    _user: dict = Depends(get_current_user),
    limit: int = 25,
) -> dict:
    """Show the last N receipts that were auto-submitted by tier-1."""
    limit = max(1, min(int(limit), 100))
    rows = await db["execution_receipts"].find(
        {"executed_by": "auto_submit_tier_1@risedual.io"},
        {"_id": 0},
    ).sort("executed_at", -1).to_list(length=limit)
    return {"receipts": rows, "count": len(rows)}


# ──────────────────────────────────────────────────────────────────────
# Tunables what-if dial (2026-02-19)
# ──────────────────────────────────────────────────────────────────────
#
# Operator wants live what-if visibility before committing to a policy
# change: "if I lowered confidence_min from 0.85 → 0.75, what would
# I actually unlock?" Without this, the only way to find out is to
# loosen the floor in prod and watch what happens — too expensive a
# discovery loop for a real-money pipeline.
#
# Logic: read every auto_submit_skipped row in the window, join with
# the original intent to get the brain's confidence, symbol, lane.
# For a set of candidate confidence_min values, count how many of
# the `low_confidence` skips would have passed that floor instead.
# Group by symbol and brain so the operator sees "lowering to 0.75
# unlocks 87 intents (35 NVDA, 28 AAL · mostly Camino)" at a glance.
#
# Pure read; no mutation. Safe to poll.

import asyncio as _asyncio  # noqa: E402
from collections import Counter as _Counter, defaultdict as _defaultdict  # noqa: E402
from datetime import timedelta  # noqa: E402

from namespaces import SHARED_INTENTS, SHARED_GATE_RESULTS  # noqa: E402


# Candidate confidence floors we surface. 0.85 is the current default;
# the others bracket below it. Above-current floors are uninteresting
# (raising filters more, never less, never unlocks).
_CANDIDATE_FLOORS = [0.80, 0.75, 0.70, 0.65, 0.60]
_TOP_N = 5  # symbols/brains shown in each what-if row


@router.get("/tunables-simulator")
async def tunables_simulator(
    _user: dict = Depends(get_current_user),
    hours: int = 24,
) -> dict:
    """What-if simulator for the auto-submit policy filters.

    Returns:
      current_confidence_min, current_allowed_lanes, current_allowed_actions
      by_skip_category:  raw counts per category in the window
      confidence_what_if: [
        { new_min, would_unlock, top_symbols, top_brains }
      ]
      lane_what_if:      [{ lane, would_unlock, top_symbols }]  // if lane currently filtered
      action_what_if:    [{ action, would_unlock, top_symbols }] // if action currently filtered
    """
    hours = max(1, min(int(hours), 168))
    policy = get_policy()
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()

    # Pull all skip rows + the joined intent payloads in parallel.
    # `shared_intents` uses `ingest_ts` for its timestamp field.
    skip_rows, intents = await _asyncio.gather(
        db[SHARED_GATE_RESULTS].find(
            {"kind": "auto_submit_skipped", "ts": {"$gte": since}},
            {"_id": 0, "intent_id": 1, "skip_category": 1, "reason": 1},
        ).to_list(length=20000),
        db[SHARED_INTENTS].find(
            {"ingest_ts": {"$gte": since}},
            {"_id": 0, "intent_id": 1, "confidence": 1, "symbol": 1,
             "lane": 1, "action": 1, "stack": 1},
        ).to_list(length=20000),
    )
    intent_by_id = {i["intent_id"]: i for i in intents if i.get("intent_id")}

    by_skip_category: _Counter[str] = _Counter()
    # Per-skip-category: list of joined intent docs for the what-if math.
    skips_by_cat: dict[str, list[dict]] = _defaultdict(list)
    for r in skip_rows:
        cat = r.get("skip_category") or "other"
        by_skip_category[cat] += 1
        intent = intent_by_id.get(r.get("intent_id"))
        if intent:
            skips_by_cat[cat].append(intent)

    # ─── confidence_min what-if ─────────────────────────────────────
    # Only `low_confidence` skips can be unlocked by lowering the floor.
    # HOLD signals are filtered by action, not confidence — moving the
    # floor doesn't help.
    current_floor = float(policy.get("confidence_min", 0.85))
    low_conf_skips = skips_by_cat.get("low_confidence", [])
    confidence_what_if: list[dict] = []
    for new_min in _CANDIDATE_FLOORS:
        if new_min >= current_floor:
            continue  # raising the floor never unlocks anything
        passing = [
            s for s in low_conf_skips
            if (s.get("confidence") or 0) >= new_min
        ]
        if not passing:
            continue
        sym_counts = _Counter(s.get("symbol") for s in passing if s.get("symbol"))
        brain_counts = _Counter(s.get("stack") for s in passing if s.get("stack"))
        confidence_what_if.append({
            "new_min": new_min,
            "would_unlock": len(passing),
            "top_symbols": sym_counts.most_common(_TOP_N),
            "top_brains": brain_counts.most_common(_TOP_N),
        })

    # ─── lane what-if ─────────────────────────────────────────────
    # `lane_filtered` skips would unlock if the operator added that
    # lane to allowed_lanes. Group what-if by lane (e.g. "options").
    lane_what_if: list[dict] = []
    current_lanes = set(policy.get("allowed_lanes") or [])
    lane_skips = skips_by_cat.get("lane_filtered", [])
    lane_groups: dict[str, list[dict]] = _defaultdict(list)
    for s in lane_skips:
        ln = s.get("lane")
        if ln and ln not in current_lanes:
            lane_groups[ln].append(s)
    for ln, group in lane_groups.items():
        sym_counts = _Counter(s.get("symbol") for s in group if s.get("symbol"))
        lane_what_if.append({
            "lane": ln,
            "would_unlock": len(group),
            "top_symbols": sym_counts.most_common(_TOP_N),
        })
    lane_what_if.sort(key=lambda r: -r["would_unlock"])

    # ─── action what-if ────────────────────────────────────────────
    # Same shape, for action_filtered skips. HOLD is excluded — adding
    # HOLD to allowed_actions doesn't make sense (no order to place).
    action_what_if: list[dict] = []
    current_actions = set(policy.get("allowed_actions") or [])
    action_skips = skips_by_cat.get("action_filtered", [])
    action_groups: dict[str, list[dict]] = _defaultdict(list)
    for s in action_skips:
        act = s.get("action")
        if act and act not in current_actions and act != "HOLD":
            action_groups[act].append(s)
    for act, group in action_groups.items():
        sym_counts = _Counter(s.get("symbol") for s in group if s.get("symbol"))
        action_what_if.append({
            "action": act,
            "would_unlock": len(group),
            "top_symbols": sym_counts.most_common(_TOP_N),
        })
    action_what_if.sort(key=lambda r: -r["would_unlock"])

    return {
        "window_hours": hours,
        "current_confidence_min": current_floor,
        "current_allowed_lanes": sorted(current_lanes),
        "current_allowed_actions": sorted(current_actions),
        "by_skip_category": dict(by_skip_category),
        "total_skipped": sum(by_skip_category.values()),
        "confidence_what_if": confidence_what_if,
        "lane_what_if": lane_what_if,
        "action_what_if": action_what_if,
    }
