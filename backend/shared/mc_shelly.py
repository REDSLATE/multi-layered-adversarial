"""MC Shelly — Mission Control's own labeled memory store.

Doctrine:
  * MC observes the brains. MC must remember what it observed.
  * Every meaningful event — intent ingested, gate pass/fail, order
    routed, position opened/closed — is recorded with the FULL roster
    snapshot at the moment of the event.
  * The unit of memory is the POSITION (DEC/EXE/GOV/ADV/OPP/AUD), not
    the brain. Brain identity is kept for audit, but training data
    correlates outcome to position.
  * Append-only. No deletes. The operator can export but not edit.
  * Storage: MongoDB `mc_shelly` collection + nightly file dump under
    `/app/backend/mc_memory/YYYY-MM-DD.jsonl`.

Position abbreviations (3-letter codes):
  STR = strategist · EXE = executor · GOV = governor
  ADV = advisor · OPP = opponent · AUD = auditor

  Legacy DEC = strategist (pre-rename code). Historical receipts that
  recorded `DEC` continue to resolve through the legacy compat layer.

Event types:
  intent_ingested      — brain pushed an intent envelope
  gate_pass            — a gate in the chain passed
  gate_fail            — a gate in the chain blocked
  order_routed         — broker accepted the order
  order_rejected       — broker rejected the order
  position_opened      — position lifecycle: first fill
  position_closed      — position lifecycle: terminal state w/ P&L
  hypothesis_request   — operator searched a ticker on HBR
  rotation             — operator rotated a seat / roster role
"""
from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from auth import get_current_user
from db import db
from namespaces import (
    BRAIN_ROSTER,
    EXECUTION_RECEIPTS,
    MC_SHELLY,
    SHARED_AUDITOR_SEAT,
    SHARED_EXECUTOR_SEAT,
    SHARED_GATE_RESULTS,
    SHARED_INTENTS,
    SHARED_OUTCOMES,
)


router = APIRouter(prefix="/mc/shelly", tags=["mc_shelly"])

# Disk persistence — one file per UTC day, append-only.
MC_MEMORY_DIR = Path("/app/backend/mc_memory")
MC_MEMORY_DIR.mkdir(parents=True, exist_ok=True)

# ─────────────────────────── position vocabulary ───────────────────────────

POSITION_CODES: dict[str, str] = {
    "strategist": "STR",
    "executor":   "EXE",
    "governor":   "GOV",
    "advisor":    "ADV",
    "opponent":   "OPP",
    "auditor":    "AUD",
    "none":       "NONE",
    # Legacy alias (pre-2026-05-24 rename) — kept so old documents that
    # stored `decider` continue to map to a code.
    "decider":    "STR",
}

EVENT_TYPES: tuple[str, ...] = (
    "intent_ingested",
    "gate_pass",
    "gate_fail",
    "order_routed",
    "order_rejected",
    "position_opened",
    "position_closed",
    "hypothesis_request",
    "rotation",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today_file_path() -> Path:
    return MC_MEMORY_DIR / f"{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.jsonl"


# ─────────────────────────── roster snapshot ───────────────────────────

async def positions_at_now() -> dict[str, Optional[str]]:
    """Return the full operator-assigned position map at this instant.

    Shape:
        {
          "STR": "barracuda" | None,
          "EXE": "camino"  | None,
          "GOV": "hellcat" | None,
          "ADV": None,
          "OPP": "gto" | None,
          "AUD": "gto" | None,
        }
    """
    roster = await db[BRAIN_ROSTER].find_one({"_id": "current"}, {"_id": 0}) or {}
    assignments = roster.get("assignments") or {}
    # Read both canonical (`strategist`) and legacy (`decider`) keys so
    # an in-flight migration doesn't blank the slot during the swap.
    strategist_holder = assignments.get("strategist") or assignments.get("decider")
    out: dict[str, Optional[str]] = {
        POSITION_CODES["strategist"]: strategist_holder,
        POSITION_CODES["executor"]:   assignments.get("executor"),
        POSITION_CODES["governor"]:   assignments.get("governor"),
        POSITION_CODES["advisor"]:    assignments.get("advisor"),
        POSITION_CODES["opponent"]:   assignments.get("opponent"),
    }
    # 2026-02-20 doctrine pin (single-source-of-truth refactor):
    # Auditor holder lives in `brain_roster.assignments.auditor` —
    # same place every other seat lives. The legacy `shared_auditor_seat`
    # doc is no longer consulted here; it's migrated to roster at boot
    # and slated for cleanup via /api/admin/seat-state/cleanup-legacy.
    out[POSITION_CODES["auditor"]] = assignments.get("auditor")
    return out


def position_of_brain(positions: dict[str, Optional[str]], brain: Optional[str]) -> str:
    """Reverse-map: given a positions snapshot + a brain, return the
    3-letter code of the position that brain occupied. NONE if the brain
    held no seat. If multiple, returns the first (STR > EXE > GOV > ADV >
    OPP > AUD) — by doctrine each brain should hold at most one role."""
    if not brain:
        return "NONE"
    for code in ("STR", "EXE", "GOV", "ADV", "OPP", "AUD"):
        if positions.get(code) == brain:
            return code
    return "NONE"


# ─────────────────────────── write API ───────────────────────────

async def record(
    *,
    event_type: str,
    brain: Optional[str],
    symbol: Optional[str] = None,
    action: Optional[str] = None,
    confidence: Optional[float] = None,
    outcome: Optional[str] = None,
    pnl_usd: Optional[float] = None,
    regime_fp: Optional[dict] = None,
    rationale: Optional[str] = None,
    error_reason: Optional[str] = None,
    ref_id: Optional[str] = None,           # intent_id / position_id / receipt_id
    gate_name: Optional[str] = None,
    extra: Optional[dict] = None,
) -> str:
    """Write one Shelly row. Returns event_id. Never raises — Shelly
    write failures must not break the operational flow that triggered
    them. Failures log to stderr.
    """
    if event_type not in EVENT_TYPES:
        # Allow unknown types — future-proof. Just log.
        pass
    positions = await positions_at_now()
    held_position = position_of_brain(positions, brain)
    event_id = str(uuid.uuid4())
    row: dict = {
        "event_id": event_id,
        "event_type": event_type,
        "ts": _now_iso(),
        "brain": brain,
        "position_at_event": held_position,
        "positions_snapshot": positions,
        "symbol": symbol,
        "action": action,
        "confidence": confidence,
        "outcome": outcome,
        "pnl_usd": pnl_usd,
        "regime_fp": regime_fp,
        "rationale": rationale,
        "error_reason": error_reason,
        "ref_id": ref_id,
        "gate_name": gate_name,
        "extra": extra or {},
    }

    # ── MongoDB (primary store) ──
    try:
        await db[MC_SHELLY].insert_one(row.copy())
    except Exception as e:  # noqa: BLE001
        print(f"[mc_shelly] mongo write failed: {e}")

    # ── File appendix (training-data substrate, daily) ──
    # Strip Mongo's potential _id mutation; use a fresh shallow copy.
    # 2026-02-19: file IO is offloaded to the default thread executor
    # so the synchronous `open(..., 'a')` + `write` doesn't block the
    # event loop. Under the auto-router's per-tick load every blocking
    # syscall compounds — moving disk IO off-loop was a measurable
    # contributor to the 15-minute prod crash.
    try:
        line = json.dumps({k: v for k, v in row.items() if k != "_id"}, default=str)
        await asyncio.get_running_loop().run_in_executor(
            None, _append_jsonl_line, _today_file_path(), line,
        )
    except Exception as e:  # noqa: BLE001
        print(f"[mc_shelly] file append failed: {e}")

    return event_id


def _append_jsonl_line(path: Path, line: str) -> None:
    """Sync helper used from `run_in_executor`. Append one line of
    JSON to the daily file. Kept tiny and exception-free at the
    boundary so the executor never carries surprises."""
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def record_async(**kwargs) -> None:
    """Fire-and-forget wrapper. Callers in hot paths use this so they
    never wait on Shelly writes.

    Holds a strong reference to each task in `_pending_tasks` —
    asyncio.create_task() only weakly references tasks, so without this
    they can be garbage-collected mid-execution. Tasks self-clean on
    completion."""
    try:
        task = asyncio.create_task(record(**kwargs))
    except RuntimeError:
        # Called outside an event loop — skip silently.
        return
    _pending_tasks.add(task)
    task.add_done_callback(_pending_tasks.discard)


# Strong-ref bucket for fire-and-forget Shelly writes. asyncio.create_task
# uses weak references; without this set, tasks can be GC'd before they
# run. `add_done_callback(discard)` keeps the set bounded.
_pending_tasks: set[asyncio.Task] = set()


# ─────────────────────────── backfill ───────────────────────────

async def backfill_from_existing(dry_run: bool = False) -> dict:
    """One-shot migration: replay existing intents / gate results /
    receipts / outcomes as MC Shelly rows. Idempotent — checks
    `backfill_ref` to skip rows already migrated.
    """
    stats = {
        "intents_ingested": 0,
        "gate_passes": 0,
        "gate_fails": 0,
        "orders_routed": 0,
        "outcomes_recorded": 0,
        "skipped_already_present": 0,
    }
    # Lookup of already-migrated refs (avoid re-inserting on re-run).
    existing_refs: set[str] = set()
    async for row in db[MC_SHELLY].find(
        {"extra.backfilled": True},
        {"_id": 0, "ref_id": 1, "event_type": 1, "gate_name": 1},
    ):
        rid = row.get("ref_id")
        et = row.get("event_type")
        gn = row.get("gate_name") or ""
        if rid:
            existing_refs.add(f"{et}:{gn}:{rid}")

    # ── intents → intent_ingested ──
    async for it in db[SHARED_INTENTS].find({}, {"_id": 0}):
        key = f"intent_ingested::{it.get('intent_id')}"
        if key in existing_refs:
            stats["skipped_already_present"] += 1
            continue
        if dry_run:
            stats["intents_ingested"] += 1
            continue
        positions_snapshot = it.get("positions_at_post") or {
            POSITION_CODES["executor"]: it.get("executor_holder_at_post"),
        }
        await db[MC_SHELLY].insert_one({
            "event_id": str(uuid.uuid4()),
            "event_type": "intent_ingested",
            "ts": it.get("ingest_ts") or _now_iso(),
            "brain": it.get("stack"),
            "position_at_event": position_of_brain(positions_snapshot, it.get("stack")),
            "positions_snapshot": positions_snapshot,
            "symbol": it.get("symbol"),
            "action": it.get("action"),
            "confidence": it.get("confidence"),
            "outcome": "executed" if it.get("executed") else "pending",
            "pnl_usd": None,
            "regime_fp": (it.get("evidence") or {}).get("regime_fp"),
            "rationale": it.get("rationale"),
            "ref_id": it.get("intent_id"),
            "extra": {"backfilled": True},
        })
        stats["intents_ingested"] += 1

    # ── gate results → gate_pass / gate_fail per gate ──
    async for gr in db[SHARED_GATE_RESULTS].find({}, {"_id": 0}):
        gates = gr.get("gates") or []
        for g in gates:
            gn = g.get("name") or "unknown"
            key = f"gate_{'pass' if g.get('passed') else 'fail'}:{gn}:{gr.get('intent_id')}"
            if key in existing_refs:
                stats["skipped_already_present"] += 1
                continue
            if dry_run:
                if g.get("passed"):
                    stats["gate_passes"] += 1
                else:
                    stats["gate_fails"] += 1
                continue
            await db[MC_SHELLY].insert_one({
                "event_id": str(uuid.uuid4()),
                "event_type": "gate_pass" if g.get("passed") else "gate_fail",
                "ts": gr.get("ts") or _now_iso(),
                "brain": None,         # gates evaluated by MC, not a brain
                "position_at_event": "NONE",
                "positions_snapshot": {},
                "symbol": None,
                "outcome": "pass" if g.get("passed") else "fail",
                "rationale": g.get("reason"),
                "ref_id": gr.get("intent_id"),
                "gate_name": gn,
                "extra": {"backfilled": True, "kind": gr.get("kind")},
            })
            if g.get("passed"):
                stats["gate_passes"] += 1
            else:
                stats["gate_fails"] += 1

    # ── execution receipts → order_routed ──
    async for rc in db[EXECUTION_RECEIPTS].find({}, {"_id": 0}):
        key = f"order_routed::{rc.get('receipt_id')}"
        if key in existing_refs:
            stats["skipped_already_present"] += 1
            continue
        if dry_run:
            stats["orders_routed"] += 1
            continue
        await db[MC_SHELLY].insert_one({
            "event_id": str(uuid.uuid4()),
            "event_type": "order_routed",
            "ts": rc.get("executed_at") or _now_iso(),
            "brain": rc.get("stack"),
            "position_at_event": "EXE",      # only the executor routes orders
            "positions_snapshot": {},
            "symbol": rc.get("symbol"),
            "action": rc.get("action"),
            "outcome": "executed",
            "pnl_usd": None,
            "ref_id": rc.get("receipt_id"),
            "extra": {
                "backfilled": True,
                "broker_order_id": rc.get("broker_order_id"),
                "notional_usd": rc.get("notional_usd"),
            },
        })
        stats["orders_routed"] += 1

    # ── brain outcomes (existing W/L from opinions) → outcome rows ──
    async for oc in db[SHARED_OUTCOMES].find({}, {"_id": 0}):
        oid = oc.get("opinion_id") or oc.get("position_id")
        if not oid:
            continue
        key = f"position_closed::{oid}"
        if key in existing_refs:
            stats["skipped_already_present"] += 1
            continue
        if dry_run:
            stats["outcomes_recorded"] += 1
            continue
        v = (oc.get("outcome") or "").lower()
        norm = "win" if v in ("win", "correct", "good") else ("loss" if v in ("loss", "wrong", "bad") else v)
        await db[MC_SHELLY].insert_one({
            "event_id": str(uuid.uuid4()),
            "event_type": "position_closed",
            "ts": oc.get("resolved_at") or _now_iso(),
            "brain": oc.get("runtime") or oc.get("resolved_by"),
            "position_at_event": "NONE",
            "positions_snapshot": {},
            "symbol": None,
            "outcome": norm,
            "pnl_usd": oc.get("pnl_usd"),
            "rationale": oc.get("rationale"),
            "ref_id": oid,
            "extra": {"backfilled": True, "source": "shared_brain_outcomes"},
        })
        stats["outcomes_recorded"] += 1

    return stats


# ─────────────────────────── HTTP API ───────────────────────────

@router.get("/")
async def list_events(
    limit: int = Query(default=100, ge=1, le=2000),
    event_type: Optional[str] = None,
    brain: Optional[str] = None,
    position: Optional[str] = Query(default=None, description="DEC|EXE|GOV|ADV|OPP|AUD|NONE"),
    symbol: Optional[str] = None,
    outcome: Optional[str] = None,
    since_hours: Optional[int] = Query(default=None, ge=1, le=24 * 365),
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    """Operator query interface — filterable list of Shelly events."""
    q: dict = {}
    if event_type:
        q["event_type"] = event_type
    if brain:
        q["brain"] = brain
    if position:
        q["position_at_event"] = position.upper()
    if symbol:
        q["symbol"] = symbol.upper()
    if outcome:
        q["outcome"] = outcome
    if since_hours:
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=since_hours)).isoformat()
        q["ts"] = {"$gte": cutoff}
    rows = await db[MC_SHELLY].find(q, {"_id": 0}).sort("ts", -1).to_list(limit)
    return {"items": rows, "count": len(rows), "query": q}


@router.get("/stats")
async def stats(
    since_hours: int = Query(default=24, ge=1, le=24 * 365),
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    """Aggregated counters by position + event_type + outcome.

    Pass rate per position = passes / (passes + fails) for events where
    `position_at_event` was held at evaluation time."""
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=since_hours)).isoformat()

    # Total counts
    pipeline_event = [
        {"$match": {"ts": {"$gte": cutoff}}},
        {"$group": {"_id": "$event_type", "count": {"$sum": 1}}},
    ]
    by_event: list[dict] = []
    async for d in db[MC_SHELLY].aggregate(pipeline_event):
        by_event.append({"event_type": d["_id"], "count": d["count"]})

    # Pass/fail by position
    pipeline_pos = [
        {"$match": {"ts": {"$gte": cutoff}, "event_type": {"$in": ["gate_pass", "gate_fail"]}}},
        {"$group": {
            "_id": {"position": "$position_at_event", "event_type": "$event_type"},
            "count": {"$sum": 1},
        }},
    ]
    pos_buckets: dict[str, dict[str, int]] = {}
    async for d in db[MC_SHELLY].aggregate(pipeline_pos):
        pos = d["_id"]["position"] or "NONE"
        et = d["_id"]["event_type"]
        pos_buckets.setdefault(pos, {"gate_pass": 0, "gate_fail": 0})[et] = d["count"]
    by_position = []
    for pos, b in pos_buckets.items():
        total = b["gate_pass"] + b["gate_fail"]
        rate = (b["gate_pass"] / total * 100) if total else None
        by_position.append({
            "position": pos,
            "passes": b["gate_pass"],
            "fails": b["gate_fail"],
            "pass_rate_pct": round(rate, 1) if rate is not None else None,
        })

    # W/L by brain
    pipeline_wl = [
        {"$match": {
            "ts": {"$gte": cutoff},
            "event_type": "position_closed",
            "outcome": {"$in": ["win", "loss"]},
        }},
        {"$group": {
            "_id": {"brain": "$brain", "outcome": "$outcome"},
            "count": {"$sum": 1},
        }},
    ]
    wl_buckets: dict[str, dict[str, int]] = {}
    async for d in db[MC_SHELLY].aggregate(pipeline_wl):
        b = d["_id"]["brain"] or "unknown"
        oc = d["_id"]["outcome"]
        wl_buckets.setdefault(b, {"win": 0, "loss": 0})[oc] = d["count"]
    wl = []
    for b, c in wl_buckets.items():
        tot = c["win"] + c["loss"]
        wl.append({
            "brain": b,
            "wins": c["win"],
            "losses": c["loss"],
            "hit_rate_pct": round(c["win"] / tot * 100, 1) if tot else None,
        })

    total = await db[MC_SHELLY].count_documents({})
    in_window = await db[MC_SHELLY].count_documents({"ts": {"$gte": cutoff}})

    return {
        "window_hours": since_hours,
        "total_events_all_time": total,
        "events_in_window": in_window,
        "by_event_type": sorted(by_event, key=lambda r: -r["count"]),
        "by_position": sorted(by_position, key=lambda r: -(r["passes"] + r["fails"])),
        "win_loss_by_brain": sorted(wl, key=lambda r: -(r["wins"] + r["losses"])),
    }


@router.get("/export.jsonl")
async def export_jsonl(
    since_hours: Optional[int] = Query(default=None, ge=1, le=24 * 365),
    event_type: Optional[str] = None,
    position: Optional[str] = None,
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    """Streaming JSONL export. One row per line, suitable for direct
    feed into training pipelines."""
    q: dict = {}
    if since_hours:
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=since_hours)).isoformat()
        q["ts"] = {"$gte": cutoff}
    if event_type:
        q["event_type"] = event_type
    if position:
        q["position_at_event"] = position.upper()

    async def _stream():
        async for row in db[MC_SHELLY].find(q, {"_id": 0}).sort("ts", 1):
            yield json.dumps(row, default=str) + "\n"

    fname = f"mc_shelly_export_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.jsonl"
    return StreamingResponse(
        _stream(),
        media_type="application/x-ndjson",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@router.post("/backfill")
async def run_backfill(
    dry_run: bool = Query(default=False, description="count only, don't write"),
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    """Replay existing intents / gate-results / receipts / outcomes
    as MC Shelly rows. Idempotent — re-running adds nothing new."""
    return {"dry_run": dry_run, "stats": await backfill_from_existing(dry_run=dry_run)}
