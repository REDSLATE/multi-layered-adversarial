"""Pipeline flow health — emission counters, outcome-attribution
health (FAIL-LOUD), and loss forensics.

GET /api/admin/pipeline/counters
GET /api/admin/pipeline/outcome_health
GET /api/admin/pipeline/forensics/large_losses
GET /api/admin/pipeline/forensics/filed
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query

from auth import get_current_user
from db import db
from shared.observability.pipeline_counters import snapshot

router = APIRouter(prefix="/admin/pipeline", tags=["pipeline"])


@router.get("/counters")
async def pipeline_counters(_user: dict = Depends(get_current_user)):  # noqa: B008
    return {"ok": True, "counters": snapshot()}


@router.get("/outcome_health")
async def outcome_health(
    hours: float = Query(168.0, gt=0, le=2160),
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    """FAIL-LOUD outcome collection & attribution health check.

    Doctrine (2026-07-28 operator directive): the Kernel cannot learn
    if completed trades are not collected, graded, and attributed to
    the originating brain. This endpoint refuses to look healthy when
    the chain is broken — `ok=false` + named alerts."""
    now = datetime.now(timezone.utc)
    since = (now - timedelta(hours=hours)).isoformat()

    entries_q = {"ok": True, "action": {"$in": ["BUY", "SHORT"]},
                 "ts": {"$gte": since}}
    entries_seen = await db["executions"].count_documents(entries_q)
    exits_submitted = await db["shared_exit_receipts"].count_documents(
        {"event": "exit_submit", "ts": {"$gte": since}})
    exits_seen = await db["shared_exit_receipts"].count_documents(
        {"event": "exit_complete", "ts": {"$gte": since}})
    resolved_outcomes = await db["shared_exit_outcomes"].count_documents(
        {"closed_at": {"$gte": since}})
    matched_round_trips = await db["shared_exit_outcomes"].count_documents(
        {"closed_at": {"$gte": since}, "attribution": "trade_id",
         "brain": {"$ne": None}})
    unmatched_exits = resolved_outcomes - matched_round_trips
    missing_economics = await db["shared_exit_outcomes"].count_documents(
        {"closed_at": {"$gte": since}, "realized_r_multiple": None})

    # Unmatched entries: filled entries with no live exit plan and no
    # resolved outcome. ≥1h grace so freshly filled positions (plan
    # adoption happens within one 20s monitor tick) don't false-alarm.
    from shared.hotpath import exit_plans as plan_store
    live = plan_store.load_live()
    live_ids = {p.get("trade_id") or p.get("origin_intent_id")
                for p in live} - {None}
    live_keys = {(p.get("lane"), p.get("symbol")) for p in live}
    outcome_ids = set(await db["shared_exit_outcomes"].distinct(
        "trade_id", {"closed_at": {"$gte": since}})) - {None}
    stale_cutoff = (now - timedelta(hours=1)).isoformat()
    entry_rows = await db["executions"].find(
        {**entries_q, "ts": {"$gte": since, "$lte": stale_cutoff}},
        {"_id": 0, "intent_id": 1, "symbol": 1, "lane": 1,
         "ts": 1, "brain": 1},
    ).sort("ts", -1).limit(2000).to_list(2000)
    unmatched_entry_rows = [
        r for r in entry_rows
        if r.get("intent_id") not in live_ids
        and r.get("intent_id") not in outcome_ids
        and (r.get("lane"), r.get("symbol")) not in live_keys
    ]
    unmatched_entries = len(unmatched_entry_rows)

    alerts: list[str] = []
    if exits_seen > 0 and resolved_outcomes == 0:
        alerts.append(
            f"PIPELINE BROKEN: {exits_seen} exit fills but ZERO resolved "
            "outcomes — nothing is reaching shared_exit_outcomes")
    if resolved_outcomes > 0 and matched_round_trips / resolved_outcomes < 0.8:
        alerts.append(
            f"ATTRIBUTION DEGRADED: only {matched_round_trips}/"
            f"{resolved_outcomes} outcomes matched to a trade_id + brain")
    if unmatched_entries > 0:
        alerts.append(
            f"{unmatched_entries} filled entries have no exit plan and no "
            "resolved outcome — the round-trip chain dropped them")
    if missing_economics > 0:
        alerts.append(
            f"{missing_economics} resolved outcomes have no "
            "realized_r_multiple (initial_risk missing at adoption)")

    return {
        "ok": not alerts,
        "status": "PASS" if not alerts else "FAIL",
        "window_hours": hours,
        "entries_seen": entries_seen,
        "exits_submitted": exits_submitted,
        "exits_seen": exits_seen,
        "matched_round_trips": matched_round_trips,
        "unmatched_entries": unmatched_entries,
        "unmatched_exits": unmatched_exits,
        "resolved_outcomes": resolved_outcomes,
        "outcomes_missing_r_multiple": missing_economics,
        "live_exit_plans": len(live),
        "unmatched_entry_examples": unmatched_entry_rows[:5],
        "alerts": alerts,
        "generated_at": now.isoformat(),
    }


@router.get("/forensics/large_losses")
async def forensics_large_losses(
    min_loss_usd: float = Query(20.0, gt=0),
    days: int = Query(30, gt=0, le=365),
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    """Autopsy for every realized loss ≥ $`min_loss_usd` in the last
    `days` days: brain, lane, regime, entry/stop/exit, Governor
    multiplier, RoadGuard verdict, data completeness, risk budget vs
    realized loss — plus every broken attribution link, named."""
    from shared.exits.forensics import large_loss_report
    return await large_loss_report(min_loss_usd=min_loss_usd, days=days)


@router.get("/forensics/filed")
async def forensics_filed(
    limit: int = Query(50, gt=0, le=500),
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    """Auto-filed forensic reports (EXCEPTIONAL_LOSS >2R doctrine)."""
    rows = await db["shared_forensic_reports"].find(
        {}, {"_id": 0},
    ).sort("filed_at", -1).limit(limit).to_list(limit)
    return {"ok": True, "count": len(rows), "reports": rows}
