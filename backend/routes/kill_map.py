"""Kill-map — full-funnel throughput view: where do trades die?

GET /api/admin/kill-map?hours=24

Phase 1 of the passage-logic doctrine (operator directive 2026-07-19):

    > The pipeline should block invalidity, not uncertainty.
    > Before converting BLOCKs into DEGRADEs, measure where
    > intents actually die.

Stages reported:
    1. pulse      — snapshots built + brain silence reasons
    2. arbiter    — arbitrations vs intents emitted (+ runtime mode)
    3. ingest     — intents created vs rejected_at_ingest
    4. gates      — gate_state distribution + top block reasons
    5. broker     — executed intents + broker fills

All reads indexed + max_time_ms bounded. Silence-reason breakdown
samples the most recent 40 receipts (unwinding a full day of
receipts would scan ~1M array entries on Atlas).
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query

from auth import get_current_user
from db import db

router = APIRouter(prefix="/admin/kill-map", tags=["kill-map"])


async def _stage_pulse(since_iso: str) -> dict:
    totals = {"pulses": 0, "snapshots_total": 0, "arbitrations": 0,
              "intents_emitted": 0, "modes": {}}
    pipe = [
        {"$match": {"started_at": {"$gte": since_iso}}},
        {"$group": {
            "_id": "$runtime_mode",
            "pulses": {"$sum": 1},
            "snaps": {"$sum": {"$ifNull": ["$snapshot_count", 0]}},
            "arbs": {"$sum": {"$ifNull": ["$arbitrations_completed", 0]}},
            "intents": {"$sum": {"$ifNull": ["$intents_emitted", 0]}},
        }},
    ]
    try:
        async for d in db["mc_pulses"].aggregate(pipe, maxTimeMS=8000):
            totals["pulses"] += d["pulses"]
            totals["snapshots_total"] += d["snaps"]
            totals["arbitrations"] += d["arbs"]
            totals["intents_emitted"] += d["intents"]
            totals["modes"][d["_id"] or "unknown"] = d["pulses"]
    except Exception as exc:  # noqa: BLE001
        totals["error"] = str(exc)[:300]

    # Per-brain opinion counts + emission-suppression tallies —
    # instrumented on the receipt 2026-07-20; pre-instrumentation
    # receipts simply contribute nothing.
    totals["opinions_by_brain"] = await _sum_dict_field(
        "opinions_by_brain", since_iso,
    )
    totals["emission_suppression"] = await _sum_dict_field(
        "arbitration_outcomes", since_iso,
    )

    # Silence reasons — sample last 40 receipts (cheap, representative).
    silence: Counter = Counter()
    try:
        cursor = db["mc_pulses"].find(
            {}, {"_id": 0, "brains_silent": 1},
        ).sort([("started_at", -1)]).max_time_ms(5000).limit(40)
        async for d in cursor:
            for b in (d.get("brains_silent") or []):
                silence[b.get("reason") or "unknown"] += 1
    except Exception:  # noqa: BLE001
        pass
    totals["silence_reasons_last_40_ticks"] = dict(silence.most_common())
    avg = totals["snapshots_total"] / totals["pulses"] if totals["pulses"] else 0
    totals["avg_snapshots_per_pulse"] = round(avg, 1)
    return totals


async def _stance_metrics(hours: int) -> dict:
    """Opinion-duplication readout: how much of the directional flow
    is the SAME stance re-asserted across consecutive 5-min buckets?

    Capped at a 24h window — mc_seats grows ~2.4k rows/hour and this
    group-by must stay cheap on Atlas. Uses the `ts` index.
    """
    capped = min(hours, 24)
    since = (datetime.now(timezone.utc) - timedelta(hours=capped)).isoformat()
    out: dict = {"window_hours_used": capped}
    groups: list[tuple[tuple, int]] = []
    unique_rows = 0
    directional_rows = 0
    flat_rows = 0
    pipe = [
        {"$match": {"ts": {"$gte": since}}},
        {"$group": {
            "_id": {"brain": "$brain", "symbol": "$symbol",
                    "direction": "$direction"},
            "n": {"$sum": 1},
        }},
    ]
    try:
        async for d in db["mc_seats"].aggregate(pipe, maxTimeMS=10000):
            n = d["n"]
            unique_rows += n
            key = d["_id"]
            if key.get("direction") in ("LONG", "SHORT"):
                directional_rows += n
                groups.append((
                    (key.get("brain"), key.get("direction"), key.get("symbol")), n,
                ))
            else:
                flat_rows += n
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)[:200]
        return out
    distinct = len(groups)
    repeated = directional_rows - distinct
    out.update({
        "unique_opinion_rows": unique_rows,
        "flat_rows": flat_rows,
        "directional_rows": directional_rows,
        "distinct_stances": distinct,
        "repeated_directional_rows": repeated,
        "pct_repeated_directional": round(
            repeated / directional_rows * 100, 1) if directional_rows else 0.0,
    })
    if groups:
        (brain, direction, symbol), n = max(groups, key=lambda g: g[1])
        out["top_repeated_stance"] = {
            "brain": brain, "direction": direction, "symbol": symbol,
            "buckets": n,
        }
        out["max_buckets_same_stance"] = n
    return out


async def _sum_dict_field(field: str, since_iso: str) -> dict:
    """Sum a `{key: int}` dict field across receipts in the window."""
    out: dict[str, int] = {}
    pipe = [
        {"$match": {"started_at": {"$gte": since_iso}, field: {"$type": "object"}}},
        {"$project": {"kv": {"$objectToArray": f"${field}"}}},
        {"$unwind": "$kv"},
        {"$group": {"_id": "$kv.k", "n": {"$sum": "$kv.v"}}},
    ]
    try:
        async for d in db["mc_pulses"].aggregate(pipe, maxTimeMS=8000):
            out[d["_id"]] = d["n"]
    except Exception as exc:  # noqa: BLE001
        out["_error"] = str(exc)[:200]
    return out


async def _stage_intents(since_iso: str) -> dict:
    out = {"intents_created": 0, "by_gate_state": {}, "by_lane": {}, "by_brain": {}}
    pipe = [
        {"$match": {"ingest_ts": {"$gte": since_iso}}},
        {"$group": {
            "_id": {"gs": "$gate_state", "lane": "$lane",
                    "brain": {"$ifNull": ["$stack_canonical", "$stack"]}},
            "n": {"$sum": 1},
        }},
    ]
    try:
        async for d in db["shared_intents"].aggregate(pipe, maxTimeMS=10000):
            gs = d["_id"].get("gs") or "unknown"
            lane = d["_id"].get("lane") or "unknown"
            brain = d["_id"].get("brain") or "unknown"
            n = d["n"]
            out["intents_created"] += n
            out["by_gate_state"][gs] = out["by_gate_state"].get(gs, 0) + n
            out["by_lane"][lane] = out["by_lane"].get(lane, 0) + n
            out["by_brain"][brain] = out["by_brain"].get(brain, 0) + n
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)[:300]
    return out


async def _stage_block_reasons(since_iso: str) -> list[dict]:
    reasons: list[dict] = []
    pipe = [
        {"$match": {
            "ingest_ts": {"$gte": since_iso},
            "gate_state": {"$in": ["blocked", "rejected_at_ingest", "advisory_only"]},
        }},
        {"$group": {
            "_id": {
                "gs": "$gate_state",
                "reason": {"$ifNull": ["$broker_reason", "(no reason)"]},
                "bucket": {"$ifNull": ["$broker_error_bucket", "-"]},
                "lane": {"$ifNull": ["$lane", "-"]},
            },
            "n": {"$sum": 1},
        }},
        {"$sort": {"n": -1}},
        {"$limit": 25},
    ]
    try:
        async for d in db["shared_intents"].aggregate(pipe, maxTimeMS=10000):
            reasons.append({
                "gate_state": d["_id"]["gs"],
                "reason": d["_id"]["reason"],
                "bucket": d["_id"]["bucket"],
                "lane": d["_id"]["lane"],
                "count": d["n"],
            })
    except Exception as exc:  # noqa: BLE001
        reasons.append({"error": str(exc)[:300]})
    return reasons


async def _stage_broker(since_iso: str) -> dict:
    out: dict = {}
    try:
        out["executed_intents"] = await db["shared_intents"].count_documents(
            {"ingest_ts": {"$gte": since_iso}, "executed": True},
            maxTimeMS=8000,
        )
    except Exception as exc:  # noqa: BLE001
        out["executed_intents"] = f"error: {str(exc)[:120]}"
    # Broker submit attempts — every auto_router attempt writes one
    # `executions` row (`ok` + `broker_status`).
    submits = {"attempts": 0, "ok": 0, "by_status": {}}
    pipe = [
        {"$match": {"ts": {"$gte": since_iso}}},
        {"$group": {
            "_id": {"ok": "$ok", "status": {"$ifNull": ["$broker_status", "-"]}},
            "n": {"$sum": 1},
        }},
        {"$sort": {"n": -1}},
        {"$limit": 20},
    ]
    try:
        async for d in db["executions"].aggregate(pipe, maxTimeMS=8000):
            n = d["n"]
            submits["attempts"] += n
            if d["_id"].get("ok"):
                submits["ok"] += n
            status = d["_id"].get("status") or "-"
            submits["by_status"][status] = submits["by_status"].get(status, 0) + n
    except Exception as exc:  # noqa: BLE001
        submits["error"] = str(exc)[:200]
    out["broker_submits"] = submits
    for coll, key in (("shared_broker_fills", "broker_fills"),
                      ("execution_receipts", "execution_receipts")):
        try:
            n = await db[coll].count_documents({}, maxTimeMS=5000)
            latest = await db[coll].find_one({}, {"_id": 0}, sort=[("_id", -1)])
            ts = None
            if latest:
                ts = (latest.get("ts") or latest.get("created_at")
                      or latest.get("filled_at") or latest.get("recorded_at"))
            out[key] = {"total_all_time": n, "latest_ts": ts}
        except Exception as exc:  # noqa: BLE001
            out[key] = {"error": str(exc)[:120]}
    return out


def _verdict(pulse: dict, intents: dict, broker: dict) -> str:
    if pulse.get("pulses", 0) == 0:
        return "DIES AT STAGE 1: no pulse receipts in window — pulse worker not running."
    if pulse.get("snapshots_total", 0) == 0:
        return "DIES AT STAGE 1: pulses tick but build ZERO snapshots — data pipeline (feeders/universe/freshness), not governance. Run pipeline-doctor."
    modes = pulse.get("modes") or {}
    supp = {k: v for k, v in (pulse.get("emission_suppression") or {}).items()
            if not k.startswith("_")}
    if pulse.get("intents_emitted", 0) == 0:
        if supp.get("suppressed_disarmed"):
            return (f"DIES AT STAGE 2: arbiter picked {supp['suppressed_disarmed']} winners but is DISARMED — "
                    "flip runtime_mode to LIVE to open the tap.")
        if set(modes) == {"DISARMED"}:
            return "DIES AT STAGE 2: arbiter is DISARMED — decisions recorded but no intents emitted. Flip runtime_mode to LIVE to open the tap."
        if supp:
            top = max(supp.items(), key=lambda kv: kv[1])
            return (f"DIES AT STAGE 2: arbiter runs but emits 0 intents — dominant suppression: "
                    f"{top[0]} ({top[1]}×). See emission_suppression breakdown.")
        return "DIES AT STAGE 2: arbiter runs but emits 0 intents (all_flat / no directional opinions). Brains aren't forming directional views — check brain thresholds, not gates."
    created = intents.get("intents_created", 0)
    if created == 0:
        return "DIES AT STAGE 3: arbiter emitted intents but none landed in shared_intents — ingest path failing (auth/lane policy). Check emit_error on mc_seats decisions."
    gs = intents.get("by_gate_state", {})
    executed = broker.get("executed_intents") or 0
    blocked = gs.get("blocked", 0) + gs.get("rejected_at_ingest", 0) + gs.get("advisory_only", 0)
    if isinstance(executed, int) and executed == 0 and blocked > 0:
        return (f"DIES AT STAGE 4: {created} intents created, {blocked} blocked, 0 executed — "
                "governance inflation confirmed. See top_block_reasons for the kill list.")
    if isinstance(executed, int) and created:
        rate = executed / created * 100
        return (f"FLOW: {created} intents → {executed} executed ({rate:.0f}% passage). "
                f"Blocked: {blocked}. See top_block_reasons for the biggest killers.")
    return "Inspect stages — mixed signals."


@router.get("")
async def kill_map(
    hours: int = Query(default=24, ge=1, le=168),
    _user: dict = Depends(get_current_user),
):
    now = datetime.now(timezone.utc)
    since_iso = (now - timedelta(hours=hours)).isoformat()
    from routes.pipeline_doctor import _stage_feeders  # noqa: WPS433
    pulse = await _stage_pulse(since_iso)
    pulse["opinion_duplication"] = await _stance_metrics(hours)
    feeders = await _stage_feeders(now)
    intents = await _stage_intents(since_iso)
    reasons = await _stage_block_reasons(since_iso)
    broker = await _stage_broker(since_iso)
    return {
        "generated_at": now.isoformat(),
        "window_hours": hours,
        "verdict": _verdict(pulse, intents, broker),
        "stage_0_feeders": feeders.get("latest_audit_per_provider", feeders),
        "stage_1_pulse": pulse,
        "stage_2_arbiter": {
            "arbitrations": pulse.pop("arbitrations"),
            "intents_emitted": pulse.pop("intents_emitted"),
            "runtime_modes_seen": pulse.pop("modes"),
            "emission_suppression": pulse.pop("emission_suppression"),
        },
        "stage_3_ingest": intents,
        "stage_4_top_block_reasons": reasons,
        "stage_5_broker": broker,
    }
