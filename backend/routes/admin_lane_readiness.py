"""Lane Readiness Diagnostic — one-shot "why isn't this lane trading?".

Doctrine pin (2026-06-26, operator-driven):
    The operator asked "Barracuda stopped equity intents — why?"
    Answering that question without a dedicated surface required
    poking at five disjoint admin endpoints + a Mongo shell. This
    endpoint folds every prerequisite that gates broker submission
    on a single lane into ONE payload:

        1. lane_execution_enabled   — operator toggle
        2. auto_submit_policy       — enabled + lane in allowed_lanes
        3. executor seat            — holder assigned, may execute
        4. broker_connected         — credentials present
        5. emission cadence         — how many intents/24h, by gate_state
        6. last 24h block reasons   — aggregated failed-gate names

    Each prerequisite returns (ok: bool, detail: str, fix: str|None).
    The top-level `ready_to_trade` is the AND of every prerequisite.
    The operator can call this on prod once and know exactly which
    switch is OFF.

Endpoint:
    GET /api/admin/lane-readiness/{lane}      lane ∈ {equity, crypto}

Auth: operator JWT.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Path

from auth import get_current_user
from db import db
from namespaces import (
    LANE_EXECUTION_TOGGLES,
    SHARED_GATE_RESULTS,
    SHARED_INTENTS,
)


router = APIRouter(tags=["admin"])

_LANES = ("equity", "crypto")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _check(ok: bool, detail: str, fix: Optional[str] = None) -> dict:
    return {"ok": bool(ok), "detail": detail, "fix": fix}


# Operator-facing fix command per `submit_skip_category`. Keys mirror
# the SKIP_CATEGORY_* constants in `shared.auto_submit_policy`.
_SKIP_FIX: dict[str, str] = {
    "policy_disabled": (
        'POST /api/admin/auto-submit/policy  {"enabled": true, ...}'
    ),
    "low_confidence": (
        'POST /api/admin/auto-submit/policy  {"confidence_min": 0.45}  '
        '(or switch to tier_2_aggressive)'
    ),
    "equity_after_hours": (
        "Wait for US RTH, or POST /api/admin/equity-extended-hours "
        '{"enabled": true}'
    ),
    "seat_authority_mismatch": (
        "Advisor brain emitting on a seat it doesn't hold. Enable "
        "CONSENSUS_MODE_ENABLED=true in env to convert these into "
        "advisor opinions instead of blocking."
    ),
    "advisor_opinion_stored": (
        "Not a block — advisor opinions are being stored for the consensus "
        "engine. The seat holder's own intent is what fires; check whether "
        "the seat holder is emitting directionally."
    ),
    "seat_vacant": (
        "Assign a seat holder via Quick Seat Switches."
    ),
    "lane_filtered": (
        'POST /api/admin/auto-submit/policy  '
        '{"allowed_lanes": ["equity", "crypto"]}'
    ),
    "brain_filtered": (
        'POST /api/admin/auto-submit/policy  {"allowed_brains": '
        '["camino", "barracuda", "hellcat", "gto"]}'
    ),
    "action_filtered": (
        "Brain is emitting HOLD/non-directional. Not auto-submittable by design."
    ),
    "already_executed": (
        "Self-race — auto-submit fired twice on the same intent. "
        "Usually transient."
    ),
    "auto_submit_failed": (
        "Intent passed `matches_tier_1` but `execution_submit` raised. "
        "Inspect `post_dry_run_outcomes.samples[].reason` and "
        "/api/admin/intents/auto-submit-failures/breakdown for the gate."
    ),
    "internal_error": (
        "Exception in the auto-submit chain. Check submit_skip_reasons "
        "for the exception_type."
    ),
    "missing_auto_submit_row": (
        "Auto-submit chain never wrote an audit row for these intents — "
        "silent leak. Check supervisor logs around the intent_ids."
    ),
    "dry_run_blocked": (
        "Intent's gate-state changed AFTER dry-run passed (race). "
        "Usually transient."
    ),
    "dry_run_pending": (
        "Dry-run task still running — benign race; auto-resolves."
    ),
    "dry_run_missing": (
        "dry_run_state never set on intent — silent leak in emit path."
    ),
    "hold_action": (
        "HOLD intent — not auto-submittable by design (informational only)."
    ),
}


def _fix_for_skip_category(category: str, lane: str) -> str:  # noqa: ARG001
    return _SKIP_FIX.get(category, "")


async def _check_lane_toggle(lane: str) -> dict:
    doc = await db[LANE_EXECUTION_TOGGLES].find_one(
        {"_id": "current"}, {"_id": 0, lane: 1, f"{lane}_updated_at": 1, f"{lane}_updated_by": 1},
    )
    enabled = bool((doc or {}).get(lane, False))
    detail = (
        f"lane_execution_enabled[{lane}] = {enabled} "
        f"(last set by {(doc or {}).get(f'{lane}_updated_by') or '—'} "
        f"at {(doc or {}).get(f'{lane}_updated_at') or '—'})"
    )
    fix = None if enabled else (
        f"POST /api/admin/execution/lane-toggles  "
        f'{{"lane":"{lane}","enabled":true}}'
    )
    return _check(enabled, detail, fix)


async def _check_auto_submit_policy(lane: str) -> dict:
    """Read the persisted auto-submit policy and confirm it accepts
    this lane. The in-memory `get_policy()` snapshot is the canonical
    truth; we also surface the raw Mongo doc for transparency."""
    from shared.auto_submit_policy import get_policy, hydrate_from_mongo

    # Hydrate first so we read the current persisted state, not
    # whatever the module saw at boot.
    try:
        await hydrate_from_mongo()
    except Exception:  # noqa: BLE001
        pass
    p = get_policy()
    enabled = bool(p.get("enabled"))
    allowed_lanes = p.get("allowed_lanes") or []
    allowed_brains = p.get("allowed_brains") or []
    conf_min = p.get("confidence_min")
    lane_allowed = lane in allowed_lanes
    overall = enabled and lane_allowed
    detail = (
        f"auto_submit policy enabled={enabled} (source={p.get('source')}), "
        f"allowed_lanes={allowed_lanes}, allowed_brains={allowed_brains}, "
        f"confidence_min={conf_min}"
    )
    if not enabled:
        fix = (
            f"POST /api/admin/auto-submit/policy  "
            f'{{"enabled":true,"allowed_lanes":["{lane}"]}}  '
            f"(operator must opt-in; default is OFF)"
        )
    elif not lane_allowed:
        fix = (
            f"POST /api/admin/auto-submit/policy  "
            f'{{"enabled":true,"allowed_lanes":{allowed_lanes + [lane]}}}'
        )
    else:
        fix = None
    return _check(overall, detail, fix)


async def _check_executor_seat(lane: str) -> dict:
    """Walk every seat eligible to execute the lane and report the
    first holder. Vacant = no authority to route. Also surface the
    Paradox v2 seat-policy floor (`confidence_min`, `max_notional_usd`)
    so the operator sees on one screen if the floor is what's killing
    intents at `below_seat_confidence_min`."""
    try:
        from shared.executor_seat import get_seat_holder, seats_with_execute
        from shared.seat_policy import seat_may_execute_lane
    except Exception as exc:  # noqa: BLE001
        return _check(False, f"seat policy import failed: {exc!r}", None)

    eligible = seats_with_execute(lane)
    holder_for_seat: dict[str, Optional[str]] = {}
    matched_seat: Optional[str] = None
    matched_holder: Optional[str] = None
    for seat in eligible:
        h = await get_seat_holder(seat)
        holder_for_seat[seat] = h
        if h and matched_seat is None:
            matched_seat = seat
            matched_holder = h

    # Paradox v2 executor seat policy (confidence_min, max_notional, …)
    # — this is the floor surfaced on `below_seat_confidence_min` blocks.
    v2_seat_id = "equity_executor" if lane == "equity" else "crypto_executor"
    v2_policy = await db["paradox_v2_seat_policy_config"].find_one(
        {"seat_id": v2_seat_id},
        {"_id": 0, "confidence_min": 1, "max_notional_usd": 1,
         "size_multiplier": 1, "enabled": 1, "autonomy_mode": 1,
         "updated_at": 1, "updated_by": 1},
    ) or {}

    policy_str = (
        f"v2_seat_policy[{v2_seat_id}]: "
        f"confidence_min={v2_policy.get('confidence_min')}, "
        f"max_notional_usd={v2_policy.get('max_notional_usd')}, "
        f"size_multiplier={v2_policy.get('size_multiplier')}, "
        f"autonomy_mode={v2_policy.get('autonomy_mode')}, "
        f"enabled={v2_policy.get('enabled')}"
    )

    if matched_seat and matched_holder and seat_may_execute_lane(matched_seat, lane):
        return _check(
            True,
            f"executor seat '{matched_seat}' held by '{matched_holder}' "
            f"(eligible_seats={eligible}). {policy_str}",
        )
    return _check(
        False,
        f"executor seat for lane='{lane}' is VACANT "
        f"(eligible_seats={eligible}, holders={holder_for_seat}). {policy_str}",
        fix=(
            f"Assign a seat holder via Quick Seat Switches UI or "
            f"POST /api/executor/rotate. One of {eligible} must hold a brain."
        ),
    )


async def _check_broker_connected(lane: str) -> dict:
    try:
        from shared.broker_router import adapter_for_lane  # noqa: WPS433
        adapter = await adapter_for_lane(lane)
    except Exception as exc:  # noqa: BLE001
        return _check(False, f"broker adapter resolve failed: {exc!r}", None)
    if adapter is None:
        return _check(
            False,
            f"no broker adapter resolved for lane='{lane}'",
            fix=(
                "Connect credentials: equity → Webull (env), "
                "crypto → Kraken (POST /api/admin/kraken/credentials)."
            ),
        )
    return _check(True, f"broker adapter resolved for lane='{lane}' (live)")


async def _emission_cadence(lane: str, hours: int) -> dict:
    """Bucket the last `hours` of intents on this lane by gate_state.
    Lets the operator see at a glance whether brains are still
    emitting and where the funnel narrows."""
    since = (_now() - timedelta(hours=hours)).isoformat()
    pipeline = [
        {"$match": {"lane": lane, "ingest_ts": {"$gte": since}}},
        {"$group": {
            "_id": {"stack": "$stack", "gate_state": "$gate_state"},
            "count": {"$sum": 1},
            "latest": {"$max": "$ingest_ts"},
        }},
        {"$sort": {"count": -1}},
    ]
    rows = await db[SHARED_INTENTS].aggregate(pipeline).to_list(None)
    by_brain: dict[str, dict[str, Any]] = {}
    total = 0
    executed = 0
    for r in rows:
        brain = r["_id"]["stack"]
        gs = r["_id"]["gate_state"]
        cnt = int(r["count"])
        total += cnt
        bucket = by_brain.setdefault(brain, {"total": 0, "states": {}, "latest": None})
        bucket["total"] += cnt
        bucket["states"][gs] = cnt
        if r["latest"] and (bucket["latest"] is None or r["latest"] > bucket["latest"]):
            bucket["latest"] = r["latest"]

    # Executed count uses the boolean field directly.
    exec_count = await db[SHARED_INTENTS].count_documents({
        "lane": lane, "ingest_ts": {"$gte": since}, "executed": True,
    })
    executed = int(exec_count)

    return {
        "window_hours": hours,
        "since": since,
        "total_intents": total,
        "executed": executed,
        "by_brain": by_brain,
    }


async def _top_block_reasons(lane: str, hours: int, limit: int = 10) -> list[dict]:
    """Aggregate the top failed-gate names from dry_run results in
    the window. Tells the operator WHICH gate is killing intents."""
    since = (_now() - timedelta(hours=hours)).isoformat()
    intent_ids = await db[SHARED_INTENTS].find(
        {"lane": lane, "ingest_ts": {"$gte": since},
         "gate_state": {"$in": ["dry_run_blocked", "blocked"]}},
        {"_id": 0, "intent_id": 1},
    ).to_list(None)
    ids = [r["intent_id"] for r in intent_ids if r.get("intent_id")]
    if not ids:
        return []

    rows = await db[SHARED_GATE_RESULTS].find(
        {"intent_id": {"$in": ids}, "kind": "dry_run"},
        {"_id": 0, "gates": 1},
    ).to_list(None)
    counter: dict[str, int] = {}
    examples: dict[str, str] = {}
    for r in rows:
        for g in r.get("gates") or []:
            if g.get("passed"):
                continue
            name = g.get("name") or "unknown"
            counter[name] = counter.get(name, 0) + 1
            if name not in examples:
                examples[name] = (g.get("reason") or "")[:300]
    ranked = sorted(counter.items(), key=lambda kv: kv[1], reverse=True)[:limit]
    return [
        {"gate": k, "fail_count": v, "example_reason": examples.get(k, "")}
        for k, v in ranked
    ]


async def _post_dry_run_outcomes(lane: str, hours: int) -> dict:
    """For every intent in the window with `gate_state='dry_run_passed'`,
    classify what happened next:
      * executed=True                              → submitted to broker
      * auto_submit_skipped with skip_category=X   → policy refused
      * auto_submit_failed                         → exception in chain
      * (no auto_submit row at all)                → silent leak

    This is THE answer to "the dry-run passes but the broker never
    sees it". It shows the operator the skip category distribution
    over the brains that got past gates.
    """
    since = (_now() - timedelta(hours=hours)).isoformat()
    passed = await db[SHARED_INTENTS].find(
        {
            "lane": lane,
            "ingest_ts": {"$gte": since},
            "gate_state": "dry_run_passed",
        },
        {"_id": 0, "intent_id": 1, "stack": 1, "symbol": 1, "action": 1,
         "confidence": 1, "executed": 1},
    ).to_list(None)
    if not passed:
        return {
            "dry_run_passed_count": 0,
            "executed_count": 0,
            "submit_skip_categories": {},
            "submit_skip_reasons": {},
            "missing_auto_submit_row": 0,
            "samples": [],
        }

    ids = [p["intent_id"] for p in passed if p.get("intent_id")]
    submit_rows = await db[SHARED_GATE_RESULTS].find(
        {
            "intent_id": {"$in": ids},
            "kind": {"$in": [
                "auto_submit_skipped",
                "auto_submit_failed",
                "auto_submit_submitted",
            ]},
        },
        {"_id": 0, "intent_id": 1, "kind": 1, "skip_category": 1,
         "reason": 1, "exception_type": 1, "exception_message": 1,
         "ts": 1, "phase": 1, "stage": 1},
    ).to_list(None)

    # Latest auto-submit row per intent wins.
    latest: dict[str, dict] = {}
    for r in submit_rows:
        iid = r["intent_id"]
        if iid not in latest or (r.get("ts") or "") > (latest[iid].get("ts") or ""):
            latest[iid] = r

    by_cat: dict[str, int] = {}
    by_reason: dict[str, int] = {}
    executed_count = 0
    missing = 0
    samples: list[dict] = []
    for p in passed:
        iid = p["intent_id"]
        rec = latest.get(iid)
        if p.get("executed"):
            executed_count += 1
            continue
        if rec is None:
            missing += 1
            if len(samples) < 5:
                samples.append({**p, "outcome": "missing_auto_submit_row"})
            continue
        kind = rec.get("kind", "")
        if kind == "auto_submit_submitted":
            executed_count += 1
            continue
        cat = rec.get("skip_category") or rec.get("phase") or rec.get("stage") or kind
        reason = rec.get("reason") or rec.get("exception_type") or "unknown"
        by_cat[cat] = by_cat.get(cat, 0) + 1
        by_reason[reason] = by_reason.get(reason, 0) + 1
        if len(samples) < 8:
            samples.append({
                **p, "outcome": kind, "skip_category": cat,
                "reason": (reason or "")[:200],
            })

    return {
        "dry_run_passed_count": len(passed),
        "executed_count": executed_count,
        "submit_skip_categories": dict(sorted(by_cat.items(), key=lambda kv: kv[1], reverse=True)),
        "submit_skip_reasons": dict(sorted(by_reason.items(), key=lambda kv: kv[1], reverse=True)[:10]),
        "missing_auto_submit_row": missing,
        "samples": samples,
    }


@router.get("/admin/lane-readiness/{lane}")
async def lane_readiness(
    lane: Literal["equity", "crypto"] = Path(...),
    hours: int = 24,
    user: dict = Depends(get_current_user),  # noqa: B008, ARG001
):
    """One-shot diagnostic: "is this lane ready to trade, and if not, what's off?".

    Returns:
        ready_to_trade: bool — AND of all prerequisite checks
        checks: { name → {ok, detail, fix} }  for each prerequisite
        emission_cadence: per-brain emission tally for the last `hours`
        top_block_reasons: ranked list of failed-gate names from dry_run

    Auth: operator JWT.
    """
    if lane not in _LANES:
        raise HTTPException(status_code=422, detail=f"lane must be one of {_LANES}")
    if hours < 1 or hours > 168:
        raise HTTPException(status_code=422, detail="hours must be in [1, 168]")

    checks = {
        "lane_execution_enabled": await _check_lane_toggle(lane),
        "auto_submit_policy": await _check_auto_submit_policy(lane),
        "executor_seat": await _check_executor_seat(lane),
        "broker_connected": await _check_broker_connected(lane),
    }
    ready = all(c["ok"] for c in checks.values())

    cadence = await _emission_cadence(lane, hours)
    post = await _post_dry_run_outcomes(lane, hours)

    # ── Single-sentence headline ────────────────────────────────────
    # Operator wants ONE answer at the top of the page, not a tile
    # collage. Priority order:
    #   1. If any prerequisite gate is OFF, name it.
    #   2. Else if intents are executing, declare READY.
    #   3. Else if intents pass dry-run but die at auto-submit, name
    #      the dominant submit_skip_category.
    #   4. Else if intents are stuck in pending/blocked at dry-run,
    #      name the dominant dry-run gate failure.
    #   5. Else "no emissions" — upstream brain not running.
    failing_check = next(((n, c) for n, c in checks.items() if not c["ok"]), None)
    top_dryrun = (await _top_block_reasons(lane, hours, limit=1)) or [{}]
    if failing_check is not None:
        name, c = failing_check
        headline = {
            "status": "BLOCKED",
            "reason": name,
            "detail": c.get("detail", ""),
            "fix": c.get("fix") or "",
            "stage": "prerequisite",
        }
    elif post.get("executed_count", 0) > 0:
        headline = {
            "status": "TRADING",
            "reason": "executed",
            "detail": (
                f"{post.get('executed_count', 0)} executed in last {hours}h "
                f"({post.get('dry_run_passed_count', 0)} passed dry-run, "
                f"{cadence.get('total_intents', 0)} total emissions)"
            ),
            "fix": "",
            "stage": "execute",
        }
    elif post.get("submit_skip_categories"):
        dom_cat, dom_count = next(iter(post["submit_skip_categories"].items()))
        sample_reason = ""
        for s in post.get("samples", []):
            if s.get("skip_category") == dom_cat:
                sample_reason = s.get("reason", "") or ""
                break
        headline = {
            "status": "BLOCKED",
            "reason": dom_cat,
            "detail": (
                f"{dom_count} intents passed dry-run then died at "
                f"`{dom_cat}` in the last {hours}h. Example reason: "
                f"{sample_reason or '(none captured)'}"
            ),
            "fix": _fix_for_skip_category(dom_cat, lane),
            "stage": "auto_submit",
        }
    elif top_dryrun and top_dryrun[0].get("gate"):
        g = top_dryrun[0]
        headline = {
            "status": "BLOCKED",
            "reason": g["gate"],
            "detail": (
                f"Every directional intent fails `{g['gate']}` at dry-run "
                f"({g['fail_count']} in last {hours}h). "
                f"Example: {g.get('example_reason', '')[:160]}"
            ),
            "fix": "",
            "stage": "dry_run",
        }
    elif cadence.get("total_intents", 0) == 0:
        headline = {
            "status": "BLOCKED",
            "reason": "no_emissions",
            "detail": (
                f"No intents emitted on this lane in {hours}h. "
                f"Brain runtimes may be off (check Native Brain Runtimes tile)."
            ),
            "fix": "Verify brains are running. See /admin/diagnostics → Native Brain Runtimes.",
            "stage": "emission",
        }
    else:
        headline = {
            "status": "UNCLEAR",
            "reason": "no_signal",
            "detail": "Intents are flowing but no decisive blocker found. Investigate samples.",
            "fix": "",
            "stage": "unknown",
        }

    return {
        "lane": lane,
        "ready_to_trade": ready,
        "headline": headline,
        "checks": checks,
        "emission_cadence": cadence,
        "post_dry_run_outcomes": post,
        "top_block_reasons": await _top_block_reasons(lane, hours),
        "as_of": _now().isoformat(),
    }


__all__ = ["router"]
