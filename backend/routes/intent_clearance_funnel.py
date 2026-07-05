"""Intent-clearance funnel — the Monday tuning tile (2026-02-17).

Answers ONE question fast: "Of everything a brain emitted in the last
N hours, exactly how many survived each gate on the way to a live
broker fill — and where's the next dam?"

Pipeline stages (in order):
    emitted            → any directional intent (BUY/SELL/SHORT/COVER)
    seat_cleared       → survived Seat's authorization
                         (`gate_state != 'advisory_only' & != 'pending'`)
    risk_sized         → Governor gave it a non-zero multiplier
                         (`risk_multiplier > 0`)
    roadguard_cleared  → RoadGuard didn't block on danger
                         (`gate_state ∈ {passed, dry_run_passed,
                                          dry_run_blocked}` — the last
                          state is the lane-execution TOGGLE and does
                          NOT count as a RoadGuard failure)
    broker_submitted   → an `executions` row exists linked by intent_id
    broker_accepted    → execution.ok == True AND no exception_msg
    filled             → execution.broker_status == 'FILLED'

For each drop between consecutive stages we surface:
    - drop_count
    - top_block_reasons (up to 3, with counts)
    - sample_intent_ids (up to 5, for the operator to `grep` in logs)

Response also carries:
    - clearance_rate (filled / emitted)
    - top_block_reason (dominant blocker across the whole funnel)
    - first_failed_stage (first stage where <100% cleared)
    - breakdowns by lane, brain, symbol, side, gate_state, reject_reason

Why NO `pipeline_receipts` join:
    That collection was empty in preview during the 2026-02-17 audit —
    it's an optional analytics writer that isn't reliably populated.
    Everything the funnel needs already lives on `shared_intents` or
    `executions`. If pipeline_receipts becomes canonical later, add a
    JOIN clause here; don't require it up front.
"""
from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Query

from auth import get_current_user
from db import db


logger = logging.getLogger("intent_clearance_funnel")

router = APIRouter(prefix="/admin", tags=["intent-clearance-funnel"])


_DIRECTIONAL_ACTIONS = ["BUY", "SELL", "SHORT", "COVER"]
_SAMPLE_LIMIT = 5

# Time-window: `ts` is None on newer intents; `ingest_ts` is the
# canonical write timestamp. Some legacy docs only have `ts`. Match
# either. We express this as a reusable clause the query builders
# fold into `$and` so multiple `$or`s can coexist without clobbering
# each other.
def _window_clause(since_iso: str) -> dict:
    return {"$or": [
        {"ingest_ts": {"$gte": since_iso}},
        {"ts":        {"$gte": since_iso}},
    ]}


def _lane_clause(lane: Optional[str]) -> dict:
    return {"lane": lane.lower()} if lane else {}


def _compose(*clauses: dict) -> dict:
    """Compose multiple query clauses into a single Mongo `$and` — the
    ONLY safe way to merge queries that each carry their own `$or`
    (dict-spread would silently overwrite `$or` keys)."""
    real = [c for c in clauses if c]
    if not real:
        return {}
    if len(real) == 1:
        return dict(real[0])
    return {"$and": real}


# ─── Stage filters (Mongo query fragments) ──────────────────────────
#
# Each stage's filter describes "intents that CLEARED this stage" as a
# STANDALONE clause. The endpoint uses `_compose(...)` to AND them
# with the window and lane filters.


def _emitted_filter() -> dict:
    """Any directional intent. HOLD intents don't attempt execution
    and are excluded from the denominator so the clearance rate isn't
    diluted by advisory HOLDs."""
    return {"action": {"$in": _DIRECTIONAL_ACTIONS}}


def _seat_cleared_filter() -> dict:
    """Seat let it through. `advisory_only` is the specific state the
    router stamps when Seat returns `verdict='pass'`. `pending` means
    Seat never resolved the intent. Both = did not clear Seat."""
    return {
        "action": {"$in": _DIRECTIONAL_ACTIONS},
        "gate_state": {"$nin": ["advisory_only", "pending"]},
    }


def _risk_sized_filter() -> dict:
    """Governor gave a non-zero size multiplier. Also honors the
    presence of `broker_error_bucket` — that field is only set by
    `_route_one` AFTER risk cleared, so its presence proves the intent
    got past this stage even if the final `gate_state` is now `blocked`
    from a broker-terminal stamp."""
    return {
        "action": {"$in": _DIRECTIONAL_ACTIONS},
        "gate_state": {"$nin": ["advisory_only", "pending"]},
        "$or": [
            {"risk_multiplier": {"$gt": 0}},
            {"broker_error_bucket": {"$exists": True}},
        ],
    }


def _roadguard_cleared_filter() -> dict:
    """RoadGuard didn't block on danger. `dry_run_blocked` (with
    lane_execution_enabled reason) counts as RoadGuard-cleared —
    RoadGuard did its job, only the operator toggle stopped it. Also
    honors `broker_error_bucket` presence for the same reason
    documented on `_risk_sized_filter` — reaching the broker implies
    everything upstream cleared."""
    return {
        "action": {"$in": _DIRECTIONAL_ACTIONS},
        "$or": [
            {
                "gate_state": {"$in": ["passed", "dry_run_passed", "dry_run_blocked"]},
                "risk_multiplier": {"$gt": 0},
            },
            {"broker_error_bucket": {"$exists": True}},
        ],
    }


async def _count(coll_name: str, query: dict) -> int:
    return await db[coll_name].count_documents(query)


async def _sample_ids(coll_name: str, query: dict, limit: int = _SAMPLE_LIMIT) -> list[str]:
    ids: list[str] = []
    cursor = db[coll_name].find(query, {"intent_id": 1, "_id": 0}).sort("_id", -1).limit(limit)
    async for d in cursor:
        v = d.get("intent_id")
        if v:
            ids.append(v)
    return ids


async def _top_reasons(coll_name: str, query: dict, reason_fields: list[str],
                       limit: int = 3) -> list[dict]:
    """Aggregate top block reasons for the intents matching `query`.
    Uses the FIRST non-null value across `reason_fields` per doc as
    the effective reason. Falls back to the `gate_state` string if
    none of the reason fields are populated (so `blocked` and
    `no_trade` still surface something instead of "unknown")."""
    pipeline = [
        {"$match": query},
        {"$project": {
            "_reason": {
                "$let": {
                    "vars": {
                        "candidates": [
                            {"$ifNull": [f"${f}", None]} for f in reason_fields
                        ] + [{"$ifNull": ["$gate_state", "unknown"]}],
                    },
                    "in": {"$first": {
                        "$filter": {
                            "input": "$$candidates",
                            "cond": {"$ne": ["$$this", None]},
                        },
                    }},
                },
            },
        }},
        {"$group": {"_id": "$_reason", "n": {"$sum": 1}}},
        {"$sort": {"n": -1}},
        {"$limit": limit},
    ]
    out: list[dict] = []
    async for r in db[coll_name].aggregate(pipeline):
        if r["_id"] is None:
            continue
        out.append({"reason": str(r["_id"])[:180], "count": r["n"]})
    return out


async def _linked_executions_count(intent_query: dict, exec_query: dict) -> int:
    """Count DISTINCT intent_ids in `executions` whose intent_id is in
    the set matching `intent_query`. Distinct-on-intent-id matters
    because a single failing intent can generate multiple `executions`
    rows (each retry attempt writes one). Without distinct-counting
    the funnel would report broker_submitted > emitted."""
    ids = []
    async for d in db["shared_intents"].find(intent_query, {"intent_id": 1, "_id": 0}):
        if d.get("intent_id"):
            ids.append(d["intent_id"])
    if not ids:
        return 0
    pipeline = [
        {"$match": {**exec_query, "intent_id": {"$in": ids}}},
        {"$group": {"_id": "$intent_id"}},
        {"$count": "n"},
    ]
    async for r in db["executions"].aggregate(pipeline):
        return int(r.get("n") or 0)
    return 0


async def _linked_execution_samples(intent_query: dict, exec_query: dict,
                                    limit: int = _SAMPLE_LIMIT) -> list[str]:
    ids: list[str] = []
    async for d in db["shared_intents"].find(intent_query, {"intent_id": 1, "_id": 0}):
        if d.get("intent_id"):
            ids.append(d["intent_id"])
    if not ids:
        return []
    out: list[str] = []
    cur = db["executions"].find(
        {**exec_query, "intent_id": {"$in": ids}},
        {"intent_id": 1, "_id": 0},
    ).sort("_id", -1).limit(limit)
    async for d in cur:
        v = d.get("intent_id")
        if v:
            out.append(v)
    return out


async def _breakdown(dim: str, base_clauses: list[dict]) -> dict:
    """Emitted-vs-broker-accepted per bucket for a single dimension.
    `base_clauses` = [window_clause, lane_clause] applied consistently
    with the main funnel so bucket totals match the top-line counts.
    The two extremes are what an operator actually wants to see at a
    glance — everything in between shows up in the main funnel."""
    field_map = {
        "lane":       "$lane",
        "brain":      "$stack",         # canonical brain name field
        "symbol":     "$symbol",
        "side":       "$action",
        "gate_state": "$gate_state",
    }
    if dim not in field_map:
        return {}

    emit_q = _compose(*base_clauses, _emitted_filter())

    # Emitted per bucket
    pipe_emit = [
        {"$match": emit_q},
        {"$group": {"_id": field_map[dim], "n": {"$sum": 1}}},
        {"$sort": {"n": -1}},
    ]
    emitted_by: dict[str, int] = {}
    async for r in db["shared_intents"].aggregate(pipe_emit):
        emitted_by[str(r["_id"])] = r["n"]

    # Broker-accepted per bucket — join with executions
    proj_field = "stack" if dim == "brain" else dim
    accepted_ids_by: dict[str, list[str]] = {k: [] for k in emitted_by}
    async for d in db["shared_intents"].find(emit_q,
                                              {"intent_id": 1, proj_field: 1, "_id": 0}):
        key = str(d.get(proj_field))
        iid = d.get("intent_id")
        if key in accepted_ids_by and iid:
            accepted_ids_by[key].append(iid)

    accepted_by: dict[str, int] = {}
    for key, iids in accepted_ids_by.items():
        if not iids:
            accepted_by[key] = 0
            continue
        n = await db["executions"].count_documents({
            "intent_id": {"$in": iids},
            "ok": True,
        })
        accepted_by[key] = n

    return {
        k: {
            "emitted": emitted_by.get(k, 0),
            "broker_accepted": accepted_by.get(k, 0),
            "clearance_rate": (
                round(accepted_by.get(k, 0) / emitted_by[k], 4)
                if emitted_by.get(k) else 0.0
            ),
        }
        for k in emitted_by
    }


@router.get("/intent-clearance-funnel")
async def intent_clearance_funnel(
    hours: int = Query(24, ge=1, le=720,
                       description="Look-back window in hours (max 30 days)."),
    lane: Optional[str] = Query(None,
                                description="Filter to a single lane (equity|crypto). Omit for both."),
    _user: dict = Depends(get_current_user),
) -> dict:
    """The Monday tuning tile. See module docstring for stage semantics."""
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=hours)
    since_iso = since.isoformat()

    base = [_window_clause(since_iso), _lane_clause(lane)]

    def _q(stage_filter_fn) -> dict:
        """Compose window + lane + stage filter into ONE `$and` — the
        only shape that survives adding another `$or` clause on top."""
        return _compose(*base, stage_filter_fn())

    # ─── Stage counts ─────────────────────────────────────────────
    n_emitted           = await _count("shared_intents",   _q(_emitted_filter))
    n_seat_cleared      = await _count("shared_intents",   _q(_seat_cleared_filter))
    n_risk_sized        = await _count("shared_intents",   _q(_risk_sized_filter))
    n_roadguard_cleared = await _count("shared_intents",   _q(_roadguard_cleared_filter))
    n_broker_submitted  = await _linked_executions_count(
        _q(_roadguard_cleared_filter),
        {},
    )
    n_broker_accepted   = await _linked_executions_count(
        _q(_roadguard_cleared_filter),
        {"ok": True},
    )
    n_filled            = await _linked_executions_count(
        _q(_roadguard_cleared_filter),
        {"ok": True, "broker_status": "FILLED"},
    )

    stage_counts = [
        ("emitted",           n_emitted),
        ("seat_cleared",      n_seat_cleared),
        ("risk_sized",        n_risk_sized),
        ("roadguard_cleared", n_roadguard_cleared),
        ("broker_submitted",  n_broker_submitted),
        ("broker_accepted",   n_broker_accepted),
        ("filled",            n_filled),
    ]

    # Monotonicity clamp: no stage may exceed its predecessor. Data-race
    # anomalies (e.g. an intent stamped `advisory_only` after already
    # touching the broker) can otherwise briefly violate this and
    # confuse the operator reading the tile. Clamp is conservative —
    # we truncate to the earlier stage's count, never upshift.
    clamped: list[tuple[str, int]] = []
    running_max = None
    for name, n in stage_counts:
        if running_max is not None and n > running_max:
            n = running_max
        clamped.append((name, n))
        running_max = n
    stage_counts = clamped
    # Also update the module-local names used below for drop math.
    _by_name = dict(clamped)
    n_seat_cleared      = _by_name["seat_cleared"]
    n_risk_sized        = _by_name["risk_sized"]
    n_roadguard_cleared = _by_name["roadguard_cleared"]
    n_broker_submitted  = _by_name["broker_submitted"]
    n_broker_accepted   = _by_name["broker_accepted"]
    n_filled            = _by_name["filled"]

    stages = []
    prev = n_emitted or 1
    first_failed_stage: Optional[str] = None
    for name, n in stage_counts:
        drop = max(0, prev - n) if name != "emitted" else 0
        drop_pct_of_prev = round(drop / prev, 4) if prev else 0.0
        clearance_rate = round(n / n_emitted, 4) if n_emitted else 0.0
        stages.append({
            "name": name,
            "count": n,
            "clearance_rate": clearance_rate,
            "drop_from_prev": drop,
            "drop_pct_of_prev": drop_pct_of_prev,
        })
        # `first_failed_stage` only meaningful when we actually emitted
        # something — otherwise "everything failed" is a lie about a
        # dataset that never existed.
        if (first_failed_stage is None and n_emitted > 0
                and name != "emitted" and drop > 0):
            first_failed_stage = name
        prev = n or 1  # avoid div-by-zero on downstream stages

    # ─── Block reasons per stage drop ─────────────────────────────
    drops = {}

    # Seat drop: emitted but DIDN'T clear seat
    seat_drop_q = _compose(*base, _emitted_filter(),
                            {"gate_state": {"$in": ["advisory_only", "pending"]}})
    drops["seat_cleared"] = {
        "count": await _count("shared_intents", seat_drop_q),
        "top_block_reasons": await _top_reasons(
            "shared_intents", seat_drop_q,
            ["seat_reason", "hold_reason"],
        ),
        "sample_intent_ids": await _sample_ids("shared_intents", seat_drop_q),
    }

    # Risk drop: seat cleared but neither risk-sized nor reached-broker
    # (i.e., stuck with risk_multiplier ≤ 0 AND no broker attempt).
    risk_drop_q = _compose(*base, _seat_cleared_filter(),
                            {"$and": [
                                {"$or": [
                                    {"risk_multiplier": {"$lte": 0}},
                                    {"risk_multiplier": None},
                                    {"risk_multiplier": {"$exists": False}},
                                ]},
                                {"broker_error_bucket": {"$exists": False}},
                            ]})
    drops["risk_sized"] = {
        "count": await _count("shared_intents", risk_drop_q),
        "top_block_reasons": await _top_reasons(
            "shared_intents", risk_drop_q,
            ["hold_reason", "dry_run_reason", "seat_reason"],
        ),
        "sample_intent_ids": await _sample_ids("shared_intents", risk_drop_q),
    }

    # RoadGuard drop: risk sized but gate_state NOT in the passed set
    # AND no broker attempt (i.e., NOT terminated by broker either).
    roadguard_drop_q = _compose(*base, _risk_sized_filter(),
                                 {"gate_state": {"$nin": ["passed", "dry_run_passed",
                                                          "dry_run_blocked"]},
                                  "broker_error_bucket": {"$exists": False}})
    drops["roadguard_cleared"] = {
        "count": await _count("shared_intents", roadguard_drop_q),
        "top_block_reasons": await _top_reasons(
            "shared_intents", roadguard_drop_q,
            ["hold_reason", "dry_run_reason"],
        ),
        "sample_intent_ids": await _sample_ids("shared_intents", roadguard_drop_q),
    }

    # Broker-submitted drop: roadguard cleared but NO execution row —
    # dominated by `dry_run_blocked` with lane-execution toggle OFF.
    dry_run_lane_toggle_q = _compose(*base, _roadguard_cleared_filter(),
                                      {"gate_state": "dry_run_blocked"})
    drops["broker_submitted"] = {
        "count": max(0, n_roadguard_cleared - n_broker_submitted),
        "top_block_reasons": await _top_reasons(
            "shared_intents", dry_run_lane_toggle_q,
            ["dry_run_reason", "hold_reason"],
        ),
        "sample_intent_ids": await _sample_ids(
            "shared_intents", dry_run_lane_toggle_q,
        ),
    }

    # Broker-accepted drop: submitted but broker rejected. Dedupe by
    # intent_id so an intent that retried 5 times before being terminated
    # counts ONCE in the top-reasons histogram — otherwise historical
    # retry-storm intents would dominate the display.
    submitted_ids: list[str] = []
    async for d in db["shared_intents"].find(_q(_roadguard_cleared_filter),
                                              {"intent_id": 1, "_id": 0}):
        if d.get("intent_id"):
            submitted_ids.append(d["intent_id"])

    rejected_reasons: Counter[str] = Counter()
    rejected_samples: list[str] = []
    seen_intent_ids: set[str] = set()
    if submitted_ids:
        cur = db["executions"].find(
            {"intent_id": {"$in": submitted_ids}, "ok": False},
            {"exception_msg": 1, "broker_response": 1, "intent_id": 1, "_id": 0},
        ).sort("_id", -1)
        async for d in cur:
            iid = d.get("intent_id")
            if not iid or iid in seen_intent_ids:
                continue
            seen_intent_ids.add(iid)
            reason = None
            if d.get("exception_msg"):
                reason = str(d["exception_msg"])[:120]
            elif isinstance(d.get("broker_response"), dict):
                reason = (str(d["broker_response"].get("msg"))
                          or str(d["broker_response"].get("error"))
                          or "broker_rejected_no_detail")[:120]
            reason = reason or "unknown_broker_rejection"
            rejected_reasons[reason] += 1
            if len(rejected_samples) < _SAMPLE_LIMIT:
                rejected_samples.append(iid)

    drops["broker_accepted"] = {
        "count": max(0, n_broker_submitted - n_broker_accepted),
        "top_block_reasons": [
            {"reason": r, "count": c} for r, c in rejected_reasons.most_common(3)
        ],
        "sample_intent_ids": rejected_samples,
    }

    # Filled drop: accepted but not filled
    drops["filled"] = {
        "count": max(0, n_broker_accepted - n_filled),
        "top_block_reasons": [],  # broker holds this info; add if we get more detail
        "sample_intent_ids": await _linked_execution_samples(
            _q(_roadguard_cleared_filter),
            {"ok": True, "broker_status": {"$ne": "FILLED"}},
        ),
    }

    # ─── Top block reason across ALL drops ────────────────────────
    global_counter: Counter[str] = Counter()
    for _, info in drops.items():
        for row in info.get("top_block_reasons", []):
            global_counter[row["reason"]] += row["count"]
    top_block_reason = None
    if global_counter:
        top_block_reason, _ = global_counter.most_common(1)[0]

    # ─── Breakdowns ───────────────────────────────────────────────
    breakdowns = {}
    for dim in ("lane", "brain", "symbol", "side", "gate_state"):
        breakdowns[dim] = await _breakdown(dim, base)

    # by_reject_reason: aggregate over ALL block reasons in this window
    reject_reason_counts: Counter[str] = Counter()
    for _, info in drops.items():
        for row in info.get("top_block_reasons", []):
            reject_reason_counts[row["reason"]] += row["count"]
    breakdowns["reject_reason"] = {
        r: {"count": c} for r, c in reject_reason_counts.most_common(20)
    }

    clearance_rate = round(n_filled / n_emitted, 4) if n_emitted else 0.0

    return {
        "window": {
            "hours": hours,
            "since": since_iso,
            "until": now.isoformat(),
            "lane_filter": lane,
        },
        "clearance_rate": clearance_rate,
        "top_block_reason": top_block_reason,
        "first_failed_stage": first_failed_stage,
        "stages": stages,
        "drops": drops,
        "breakdowns": breakdowns,
    }
