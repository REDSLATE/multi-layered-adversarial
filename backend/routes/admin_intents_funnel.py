"""Intent Funnel — 7-stage execution funnel (2026-02-21).

Operator pain-point recap: the existing `post-mortem` aggregator
answers "where did intents die in aggregate" using a 5-stage funnel
(emitted → dry_run_passed → shelly_eligible → submitted → executed).
That funnel was built around the OLD chain. The new Unified Execution
Pipeline now has exactly five layered checkpoints:

    Brain → Seat → Governor → RoadGuard → AutoSubmit → Broker → Fill

Each layer is either an authority (Seat, RoadGuard, Broker) or a
modifier (Governor) or an attempt (AutoSubmit). This endpoint
collapses that into the seven canonical stages the operator wants on
the UI tile:

    1. Emitted              — shared_intents in window
    2. Seat approved        — pipeline_receipts past the seat gate
    3. Governor sized       — past governor (HOLD/ABSTAIN drop here)
    4. RoadGuard passed     — past RoadGuard
    5. Auto-submit attempted — broker_called=True (incl. errors)
    6. Broker accepted      — final_status == SUBMITTED
    7. Filled               — shared_intents.executed = True

The endpoint also surfaces the biggest stage-to-stage drop so the
operator can see WHERE the funnel is leaking without scrolling.

Stage-shift detection (2026-02-21 enhancement): each call snapshots
the current biggest-drop stage to `funnel_drop_snapshots`. If the
stage changed vs the previous snapshot for the same window AND the
previous snapshot is at least `MIN_SHIFT_GAP_SECONDS` old, the
response includes a `stage_shift` field. Operator sees a banner the
moment the leak moves from Seat to RoadGuard (or wherever) — that's
almost always a new bug.

Doctrine: this endpoint is READ-ONLY w.r.t. the execution path. It
does NOT modify `admin_intents_post_mortem.py` (which is frozen
during the production market-day observation window). It is a NEW
companion route.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends

from auth import get_current_user
from db import db
from namespaces import SHARED_INTENTS
from shared.pipeline.receipts import PIPELINE_RECEIPTS_COLL


router = APIRouter(prefix="/admin/intents", tags=["admin-intents"])

# ── Stage-shift snapshot config ─────────────────────────────────────
FUNNEL_SNAPSHOTS_COLL = "funnel_drop_snapshots"
# Minimum age of the previous snapshot before we'll consider its
# stage stale enough to flag a shift. Filters out two-polls-in-quick-
# succession noise where the underlying data hasn't actually moved.
MIN_SHIFT_GAP_SECONDS = 60
# Hard cap on retained snapshot history (best-effort; index handles it).
SNAPSHOT_RETENTION_HOURS = 72


# ── Stage classification per pipeline_receipt ───────────────────────
# Returns the *highest* stage the intent reached. A `None` means we
# can't even prove it reached the seat (no receipt — likely a brain
# that never went through the unified pipeline).
def _stage_reached(receipt: Dict[str, Any]) -> int:
    """0=not_reached, 1=emitted_only, 2=seat_approved, 3=governor,
    4=roadguard_passed, 5=auto_submit_attempted, 6=broker_accepted.
    Stage 7 (Filled) is sourced from `shared_intents.executed`.
    """
    status = (receipt.get("final_status") or "").upper()
    source = (receipt.get("restriction_source") or "").lower()
    broker_called = bool(receipt.get("broker_called"))

    # Brain HOLD/ABSTAIN — never reached the seat as an executable
    # candidate. Counts as emitted only.
    if status == "NO_ORDER":
        return 1

    # Seat refused.
    if status == "BLOCKED" and source == "seat":
        return 1

    # Made it past seat. Governor never blocks, so once past seat
    # we have "governor sized" by construction.
    if status == "BLOCKED" and source == "roadguard":
        return 3  # past seat + governor, blocked at roadguard

    # DECISION_LOGGED = observe/shadow seat mode → past seat +
    # governor + roadguard, but no broker call by design.
    if status == "DECISION_LOGGED":
        return 4

    # Broker called.
    if status == "SUBMITTED":
        return 6
    if status == "BROKER_ERROR":
        return 5  # attempted, but broker did not accept

    # Anything else: if broker_called true, count as attempted;
    # otherwise count as past roadguard at minimum.
    if broker_called:
        return 5
    return 4


@router.get("/funnel")
async def intents_funnel(
    hours: int = 24,
    _user: dict = Depends(get_current_user),  # noqa: B008
) -> Dict[str, Any]:
    """7-stage execution funnel over the last N hours.

    Args:
        hours: window depth (default 24, min 1, max 168).

    Returns:
        {
          "window_hours": int,
          "total_intents": int,
          "stages": [
            {"name": "Emitted", "key": "emitted", "count": int, "pct": float, "drop_pct": float},
            {"name": "Seat approved", ...},
            ...
          ],
          "biggest_drop": {
            "from": "RoadGuard passed",
            "to": "Auto-submit attempted",
            "lost": int,
            "drop_pct": float
          } | None,
          "by_lane": { "equity": {...stages...}, "crypto": {...} },
          "by_brain": { "camino": {...}, ... },
          "no_receipt_count": int,  # intents with no pipeline_receipt row
          "fetched_at": iso
        }
    """
    hours = max(1, min(int(hours or 24), 168))
    cutoff_iso = (
        datetime.now(timezone.utc) - timedelta(hours=hours)
    ).isoformat()

    # 1) Pull intents in window.
    intents = await db[SHARED_INTENTS].find(
        {"ingest_ts": {"$gte": cutoff_iso}},
        {
            "_id": 0, "intent_id": 1, "stack": 1, "lane": 1, "action": 1,
            "symbol": 1, "ingest_ts": 1, "executed": 1,
        },
    ).to_list(length=50000)

    intent_ids = [i["intent_id"] for i in intents]

    # 2) Pull pipeline receipts for those intents.
    receipts: List[Dict[str, Any]] = []
    if intent_ids:
        receipts = await db[PIPELINE_RECEIPTS_COLL].find(
            {"intent_id": {"$in": intent_ids}},
            {
                "_id": 0, "intent_id": 1, "final_status": 1,
                "restriction_source": 1, "broker_called": 1,
            },
        ).to_list(length=50000)
    receipts_by_id: Dict[str, Dict[str, Any]] = {
        r["intent_id"]: r for r in receipts
    }

    # 3) Classify and bucket per lane / per brain.
    # Each intent counts toward EVERY stage it reached (so the funnel
    # is monotonically non-increasing).
    stage_counts = [0] * 7  # indices 0..6 map to stages 1..7
    by_lane: Dict[str, List[int]] = {}
    by_brain: Dict[str, List[int]] = {}
    no_receipt = 0

    for it in intents:
        lane = (it.get("lane") or "unknown").lower()
        brain = (it.get("stack") or "unknown").lower()
        if lane not in by_lane:
            by_lane[lane] = [0] * 7
        if brain not in by_brain:
            by_brain[brain] = [0] * 7

        # Stage 1 — emitted (always).
        stage_counts[0] += 1
        by_lane[lane][0] += 1
        by_brain[brain][0] += 1

        r = receipts_by_id.get(it["intent_id"])
        if not r:
            # Legacy / non-unified path. We have no canonical proof
            # of seat/governor/roadguard outcome from pipeline_receipts.
            # If the intent is `executed=True` we still credit Filled
            # (the operator's ground truth: the broker confirmed).
            no_receipt += 1
            if it.get("executed"):
                for s in range(1, 7):
                    stage_counts[s] += 1
                    by_lane[lane][s] += 1
                    by_brain[brain][s] += 1
            continue

        reached = _stage_reached(r)
        # `reached` is a 1-indexed stage count (1=Emitted, 2=Seat
        # approved, …). We already credited index 0 (Emitted) above;
        # now credit indices 1..reached-1 inclusive.
        for s in range(1, reached):
            if s >= 7:
                break
            stage_counts[s] += 1
            by_lane[lane][s] += 1
            by_brain[brain][s] += 1

        # Stage 7 — Filled — sourced from `executed` flag.
        if it.get("executed"):
            stage_counts[6] += 1
            by_lane[lane][6] += 1
            by_brain[brain][6] += 1

    # Defensive monotonicity: stages must be non-increasing. The
    # `executed` flag is the only one we don't compute from receipts,
    # so cap Filled at the previous stage. Operator never sees a
    # confusing "Filled > Broker accepted" line.
    for s in range(1, 7):
        if stage_counts[s] > stage_counts[s - 1]:
            stage_counts[s] = stage_counts[s - 1]
    for lane_counts in by_lane.values():
        for s in range(1, 7):
            if lane_counts[s] > lane_counts[s - 1]:
                lane_counts[s] = lane_counts[s - 1]
    for brain_counts in by_brain.values():
        for s in range(1, 7):
            if brain_counts[s] > brain_counts[s - 1]:
                brain_counts[s] = brain_counts[s - 1]

    # 4) Shape the response.
    stage_names = [
        ("emitted",                "Emitted"),
        ("seat_approved",          "Seat approved"),
        ("governor_sized",         "Governor sized"),
        ("roadguard_passed",       "RoadGuard passed"),
        ("auto_submit_attempted",  "Auto-submit attempted"),
        ("broker_accepted",        "Broker accepted"),
        ("filled",                 "Filled"),
    ]
    total = stage_counts[0]
    stages: List[Dict[str, Any]] = []
    biggest_drop = None
    worst_lost = -1
    for i, (key, name) in enumerate(stage_names):
        count = stage_counts[i]
        pct_of_total = (100.0 * count / total) if total else 0.0
        drop_from_prev = 0
        drop_pct = 0.0
        if i > 0:
            prev = stage_counts[i - 1]
            drop_from_prev = prev - count
            drop_pct = (100.0 * drop_from_prev / prev) if prev else 0.0
            if drop_from_prev > worst_lost:
                worst_lost = drop_from_prev
                biggest_drop = {
                    "from": stage_names[i - 1][1],
                    "to": name,
                    "lost": drop_from_prev,
                    "drop_pct": round(drop_pct, 2),
                }
        stages.append({
            "key": key,
            "name": name,
            "count": count,
            "pct_of_total": round(pct_of_total, 2),
            "drop_from_prev": drop_from_prev,
            "drop_pct": round(drop_pct, 2),
        })

    def _bucket_dict(buckets: Dict[str, List[int]]) -> Dict[str, Dict[str, int]]:
        out: Dict[str, Dict[str, int]] = {}
        for k, counts in buckets.items():
            out[k] = {stage_names[i][0]: counts[i] for i in range(7)}
        return out

    return {
        "window_hours": hours,
        "total_intents": total,
        "stages": stages,
        "biggest_drop": biggest_drop,
        "stage_shift": await _record_and_detect_shift(hours, biggest_drop),
        "by_lane": _bucket_dict(by_lane),
        "by_brain": _bucket_dict(by_brain),
        "no_receipt_count": no_receipt,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


# ── Stage-shift detection ───────────────────────────────────────────
async def _record_and_detect_shift(
    hours: int,
    biggest_drop: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Snapshot the current biggest-drop stage and return a
    `stage_shift` payload iff the to-stage changed vs the previous
    snapshot for this window AND the previous snapshot is at least
    `MIN_SHIFT_GAP_SECONDS` old.

    Why a gap requirement? Two polls 5 seconds apart can flap on
    very small populations. The gap forces a genuine change.
    """
    now = datetime.now(timezone.utc)
    current_to = (biggest_drop or {}).get("to")

    # Read latest prior snapshot for this window.
    prev = await db[FUNNEL_SNAPSHOTS_COLL].find_one(
        {"window_hours": hours},
        sort=[("captured_at", -1)],
    )

    # Always write the new snapshot, even when the stage didn't shift.
    # This gives us a continuous history the operator can audit later.
    await db[FUNNEL_SNAPSHOTS_COLL].insert_one({
        "window_hours": hours,
        "biggest_drop_to": current_to,
        "biggest_drop_from": (biggest_drop or {}).get("from"),
        "biggest_drop_lost": (biggest_drop or {}).get("lost"),
        "biggest_drop_pct": (biggest_drop or {}).get("drop_pct"),
        "captured_at": now,
    })

    # Best-effort retention prune (cheap, no race risk on read).
    try:
        cutoff = now - timedelta(hours=SNAPSHOT_RETENTION_HOURS)
        await db[FUNNEL_SNAPSHOTS_COLL].delete_many(
            {"captured_at": {"$lt": cutoff}}
        )
    except Exception:
        # Retention is housekeeping — never fail the request for it.
        pass

    if not prev or not current_to or not prev.get("biggest_drop_to"):
        return None
    if prev["biggest_drop_to"] == current_to:
        return None
    # Tolerate naive vs aware datetime values defensively.
    prev_ts = prev.get("captured_at")
    if isinstance(prev_ts, datetime):
        if prev_ts.tzinfo is None:
            prev_ts = prev_ts.replace(tzinfo=timezone.utc)
        gap = (now - prev_ts).total_seconds()
    else:
        gap = MIN_SHIFT_GAP_SECONDS  # unknown → permit
    if gap < MIN_SHIFT_GAP_SECONDS:
        return None

    return {
        "from_stage": prev["biggest_drop_to"],
        "to_stage": current_to,
        "prev_captured_at": prev_ts.isoformat() if isinstance(prev_ts, datetime) else None,
        "gap_seconds": int(gap),
    }
