"""Paradox v3 — admin observability endpoints (2026-02, Step 5).

Surfaces the operator-facing status of the v3 rollout in two routes:

  GET /api/admin/paradox-v3/status
      Both env flags + lifter-vs-emit posture so the operator can
      see at a glance which brains are on v3 and whether the
      trigger watcher is live.

  GET /api/admin/paradox-v3/watch-queue
      Watch-queue snapshot — state counts + the most-recent N rows.
      Safe to call when the watcher is dormant (read-only).

Doctrine: read-only. No writes, no broker calls, no env mutation.
"""
from __future__ import annotations

import os
from typing import Any, Dict

from fastapi import APIRouter, Depends, Query

from auth import get_current_user
from shared.pipeline.trigger_watcher import (
    is_refire_enabled,
    is_watcher_enabled,
    watch_queue_snapshot,
)


router = APIRouter(prefix="/admin/paradox-v3", tags=["admin-paradox-v3"])


@router.get("/status")
async def paradox_v3_status(
    _user: dict = Depends(get_current_user),  # noqa: B008
) -> Dict[str, Any]:
    """One-stop rollout status: which brains are on v3, whether the
    trigger watcher is live, and the in-process feature-flag posture.

    Operator workflow:
      * Empty `brains_on_v3` + watcher off → Steps 1-4 shipped, no
        brain emitting v3 yet. Use this to confirm posture BEFORE
        flipping camino on.
      * `brains_on_v3=["camino"]` + watcher off → Step 4 shadow
        running. Wait 24h, check `plan_discipline` axis on Camino's
        report card.
      * `brains_on_v3=["camino"]` + watcher on → Step 5 LIVE.
        Camino's WAIT_FOR_TRIGGER plans land on the queue; the
        watcher fires/invalidates/expires them.

    Lane posture (2026-02-22): SeatPolicy's auth gates run BEFORE
    the WAIT short-circuit (defensive doctrine). A vacant executor
    seat for a lane means WAIT plans on THAT lane cannot be parked.
    `lane_executor_seats` surfaces the current holder so the operator
    knows which lanes are eligible for v3 WAIT plans.
    """
    brains_csv = os.environ.get("PARADOX_V3_BRAINS", "").strip()
    brains_on_v3 = (
        sorted({b.strip().lower() for b in brains_csv.split(",") if b.strip()})
        if brains_csv else []
    )

    # Lane-aware seat-holder posture (read-only, defensive).
    from db import db
    from namespaces import BRAIN_ROSTER
    lane_seats: Dict[str, Any] = {
        "equity": {"executor_holder": None, "wait_plans_eligible": False},
        "crypto": {"executor_holder": None, "wait_plans_eligible": False},
    }
    try:
        roster = await db[BRAIN_ROSTER].find_one(
            {"_id": "current"}, {"_id": 0, "assignments": 1},
        ) or {}
        assignments = roster.get("assignments") or {}
        equity_holder = assignments.get("executor")
        crypto_holder = assignments.get("crypto")
        lane_seats["equity"]["executor_holder"] = equity_holder
        lane_seats["equity"]["wait_plans_eligible"] = bool(equity_holder)
        lane_seats["crypto"]["executor_holder"] = crypto_holder
        lane_seats["crypto"]["wait_plans_eligible"] = bool(crypto_holder)
    except Exception:  # noqa: BLE001
        pass

    return {
        "brains_on_v3": brains_on_v3,
        "trigger_watcher_enabled": is_watcher_enabled(),
        "trigger_refire_enabled": is_refire_enabled(),
        "flags": {
            "PARADOX_V3_BRAINS": brains_csv or None,
            "PARADOX_V3_TRIGGER_WATCHER": (
                os.environ.get("PARADOX_V3_TRIGGER_WATCHER") or None
            ),
            "PARADOX_V3_TRIGGER_REFIRE": (
                os.environ.get("PARADOX_V3_TRIGGER_REFIRE") or None
            ),
        },
        "rollout_step": _infer_rollout_step(
            brains_on_v3, is_watcher_enabled(), is_refire_enabled(),
        ),
        "lane_executor_seats": lane_seats,
        "doctrine_note": (
            "Step 5 LIVE = at least one brain in `brains_on_v3` AND "
            "`trigger_watcher_enabled=true`. Step 5.b REFIRE = the "
            "above plus `trigger_refire_enabled=true` — fired plans "
            "translate into actual broker calls. Step 4 SHADOW = "
            "brains on v3 but watcher still off. Steps 1-3 = no "
            "brain on v3. WAIT plans can only be parked on lanes "
            "whose `wait_plans_eligible=true` (executor seat held)."
        ),
    }


def _infer_rollout_step(
    brains: list[str], watcher_live: bool, refire_live: bool,
) -> str:
    if not brains:
        return "steps_1_to_3_rails_only"
    if brains and not watcher_live:
        return "step_4_shadow_emit_only"
    if watcher_live and not refire_live:
        return "step_5_trigger_watcher_live"
    return "step_5b_refire_live"


@router.get("/watch-queue")
async def paradox_v3_watch_queue(
    limit: int = Query(default=50, ge=1, le=500),
    _user: dict = Depends(get_current_user),  # noqa: B008
) -> Dict[str, Any]:
    """Read-only snapshot of `intent_watch_queue`.

    Returns:
        {
            "enabled":   bool,                   # watcher env flag
            "counts":    {watching, fired, invalidated, expired},
            "recent":    [last N rows desc by queued_at, _id stripped],
            "fetched_at": iso
        }

    Even when the watcher is dormant this endpoint is useful — it
    surfaces any backlog the operator would drain by flipping the
    flag on.
    """
    return await watch_queue_snapshot(limit=limit)


# ── Step 7+ execution_style_outcomes (operator pin 2026-02-22) ─────
# Confidence bands per operator's recommended thresholds. The endpoint
# returns the per-style state alongside the raw counts so the
# frontend tile can colour rows by band without re-implementing the
# thresholds. Bands are intentionally CONSERVATIVE — execution
# heuristics are noisy; 200 trades for HIGH_CONVICTION before
# replacing a heuristic mirrors the doctrine pin at PRD §13 step 7.
_BANDS = (
    ("HIGH_CONVICTION", 200),
    ("STRONG",          100),
    ("READY",            50),
    ("LEARNING",         30),
)


def _band_for_samples(n: int) -> str:
    for name, floor in _BANDS:
        if n >= floor:
            return name
    return "INSUFFICIENT"


@router.get("/execution-style-outcomes")
async def execution_style_outcomes(
    _user: dict = Depends(get_current_user),  # noqa: B008
) -> Dict[str, Any]:
    """Per-execution-style win-rate + avg-return for v3 plans.

    Reads `doctrine_sidecars` (audit rows joined with bracket outcomes
    via `outcome_join`), filters to `intent_version="v3"`, groups by
    `plan_execution_style`, and emits one row per style with:

        * trades          — count of resolved outcomes
        * wins            — count of outcome_label="win"
        * losses          — count of outcome_label∈{loss,stopped_out}
        * win_rate        — wins / (wins+losses), null when none resolved
        * avg_pnl_usd     — mean pnl_usd across resolved rows
        * state           — confidence band (per `_BANDS`)

    Doctrine pin (operator 2026-02-22): bands are CONSERVATIVE.
    Execution heuristics are notoriously noisy; `HIGH_CONVICTION`
    requires ≥200 trades before any heuristic replacement signal is
    considered strong. The hard floor is 30 — below that a style
    reads `INSUFFICIENT`.

    Read-only. Safe to call frequently — the tile polls every 10s.
    """
    from db import db
    from namespaces import DOCTRINE_SIDECARS

    cursor = db[DOCTRINE_SIDECARS].find(
        {
            "intent_version": "v3",
            "outcome_join": {"$exists": True},
        },
        {
            "_id": 0,
            "plan_execution_style": 1,
            "outcome_join.outcome_label": 1,
            "outcome_join.pnl_usd": 1,
        },
    )

    bucket: Dict[str, Dict[str, Any]] = {}
    async for row in cursor:
        style = (row.get("plan_execution_style") or "UNKNOWN").upper()
        oj = row.get("outcome_join") or {}
        label = (oj.get("outcome_label") or "").lower()
        pnl = float(oj.get("pnl_usd") or 0.0)
        b = bucket.setdefault(style, {
            "trades": 0, "wins": 0, "losses": 0, "pnl_sum": 0.0,
        })
        b["trades"] += 1
        b["pnl_sum"] += pnl
        if label == "win":
            b["wins"] += 1
        elif label in ("loss", "stopped_out"):
            b["losses"] += 1

    styles_out = []
    for style, b in sorted(bucket.items()):
        resolved = b["wins"] + b["losses"]
        win_rate = (b["wins"] / resolved) if resolved else None
        avg_pnl = (b["pnl_sum"] / b["trades"]) if b["trades"] else 0.0
        styles_out.append({
            "execution_style": style,
            "trades":          b["trades"],
            "wins":            b["wins"],
            "losses":          b["losses"],
            "win_rate":        (round(win_rate, 4) if win_rate is not None else None),
            "avg_pnl_usd":     round(avg_pnl, 4),
            "state":           _band_for_samples(b["trades"]),
        })

    return {
        "styles": styles_out,
        "bands": {name: floor for name, floor in _BANDS},
        "hard_floor": 30,
        "doctrine_note": (
            "Execution heuristics are notoriously noisy. Conservative "
            "bands: LEARNING≥30, READY≥50, STRONG≥100, HIGH_CONVICTION"
            "≥200. Don't replace a heuristic until at least STRONG."
        ),
    }


# ── Per-brain execution-style profile (operator pin 2026-02-23) ────
# OBSERVATIONAL PROFILE. The seat-doctrinal canonicalization pin
# (intents.py:696) says `stack` is METADATA only — metrics keyed on
# `stack` must NEVER imply "brain X underperformed", only "(seat,
# lane, doctrine_version) outcomes while X occupied the seat".
#
# This endpoint surfaces a brain × execution_style cross-tab so the
# operator can spot METADATA correlations (e.g., "camino's PATIENT
# plans had 8 outcomes — still INSUFFICIENT") WITHOUT scoring brains
# off it. Same conservative bands as `execution-style-outcomes`.
#
# Read-only. Polled every 10s by the dashboard tile.
@router.get("/per-brain-execution-style-profile")
async def per_brain_execution_style_profile(
    _user: dict = Depends(get_current_user),  # noqa: B008
) -> Dict[str, Any]:
    """Per-brain × per-execution-style observational profile.

    Returns a matrix-friendly shape:

        {
          "brains":  ["camino", "barracuda", ...],   # rows
          "styles":  ["MARKET_NOW", "PATIENT", ...], # cols
          "cells":   [{brain, execution_style, trades, wins,
                       losses, win_rate, avg_pnl_usd, state}, ...],
          "totals_by_brain": [{brain, trades, wins, losses,
                               win_rate, avg_pnl_usd}, ...],
          "bands":   {HIGH_CONVICTION:200, STRONG:100, ...},
          "hard_floor": 30,
          "doctrine_note": "OBSERVATIONAL — stack is metadata...",
        }

    Filtering: `intent_version="v3"` AND `outcome_join` joined.
    Rows missing a `stack` value bucket under "UNKNOWN".
    """
    from db import db
    from namespaces import DOCTRINE_SIDECARS

    cursor = db[DOCTRINE_SIDECARS].find(
        {
            "intent_version": "v3",
            "outcome_join": {"$exists": True},
        },
        {
            "_id": 0,
            "stack": 1,
            "plan_execution_style": 1,
            "outcome_join.outcome_label": 1,
            "outcome_join.pnl_usd": 1,
        },
    )

    # bucket[(brain, style)] = {trades, wins, losses, pnl_sum}
    bucket: Dict[tuple, Dict[str, Any]] = {}
    brains_seen: set = set()
    styles_seen: set = set()
    async for row in cursor:
        brain = (row.get("stack") or "UNKNOWN").lower() or "UNKNOWN"
        style = (row.get("plan_execution_style") or "UNKNOWN").upper()
        oj = row.get("outcome_join") or {}
        label = (oj.get("outcome_label") or "").lower()
        pnl = float(oj.get("pnl_usd") or 0.0)
        brains_seen.add(brain)
        styles_seen.add(style)
        key = (brain, style)
        b = bucket.setdefault(key, {
            "trades": 0, "wins": 0, "losses": 0, "pnl_sum": 0.0,
        })
        b["trades"] += 1
        b["pnl_sum"] += pnl
        if label == "win":
            b["wins"] += 1
        elif label in ("loss", "stopped_out"):
            b["losses"] += 1

    cells = []
    for (brain, style), b in sorted(bucket.items()):
        resolved = b["wins"] + b["losses"]
        win_rate = (b["wins"] / resolved) if resolved else None
        avg_pnl = (b["pnl_sum"] / b["trades"]) if b["trades"] else 0.0
        cells.append({
            "brain":           brain,
            "execution_style": style,
            "trades":          b["trades"],
            "wins":            b["wins"],
            "losses":          b["losses"],
            "win_rate":        (round(win_rate, 4) if win_rate is not None else None),
            "avg_pnl_usd":     round(avg_pnl, 4),
            "state":           _band_for_samples(b["trades"]),
        })

    # Per-brain row totals across styles — gives operator a quick
    # sense of which brain has accumulated enough v3 outcomes overall.
    totals_buf: Dict[str, Dict[str, Any]] = {}
    for c in cells:
        t = totals_buf.setdefault(c["brain"], {
            "brain":   c["brain"],
            "trades":  0, "wins": 0, "losses": 0, "pnl_sum": 0.0,
        })
        t["trades"] += c["trades"]
        t["wins"]   += c["wins"]
        t["losses"] += c["losses"]
        t["pnl_sum"] += c["avg_pnl_usd"] * c["trades"]
    totals_by_brain = []
    for brain, t in sorted(totals_buf.items()):
        resolved = t["wins"] + t["losses"]
        win_rate = (t["wins"] / resolved) if resolved else None
        avg_pnl = (t["pnl_sum"] / t["trades"]) if t["trades"] else 0.0
        totals_by_brain.append({
            "brain":       brain,
            "trades":      t["trades"],
            "wins":        t["wins"],
            "losses":      t["losses"],
            "win_rate":    (round(win_rate, 4) if win_rate is not None else None),
            "avg_pnl_usd": round(avg_pnl, 4),
            "state":       _band_for_samples(t["trades"]),
        })

    return {
        "brains":          sorted(brains_seen),
        "styles":          sorted(styles_seen),
        "cells":           cells,
        "totals_by_brain": totals_by_brain,
        "bands":           {name: floor for name, floor in _BANDS},
        "hard_floor":      30,
        "doctrine_note": (
            "OBSERVATIONAL PROFILE — `stack` is METADATA per the "
            "seat-doctrinal canonicalization pin (intents.py:696). "
            "Cells show outcomes WHILE a brain occupied the seat, "
            "not a brain scoring axis. Conservative bands apply "
            "(LEARNING≥30, READY≥50, STRONG≥100, HIGH_CONVICTION≥200)."
        ),
    }
