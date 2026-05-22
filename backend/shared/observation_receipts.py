"""Observation Receipts — graded learning samples from "honest hold" intents.

Doctrine pin (2026-02-18, supersedes prior "no observation samples" stance):
    The original separation was "real fills → doctrine expectancy, no
    observations pollute the sample set". That doctrine produced a
    deadlock: the brain's "honest hold" path (display=BUY but
    `size_multiplier=0` and `would_trade_without_gates=false`)
    generated ~100 intents/hr but ZERO learnable outcomes. Doctrines
    stayed at LEARNING 0/100 for months; only 3 days of real fills
    after months of operation.

    The ladder fix:

        INTENT
          → OBSERVATION RECEIPT     (gates pass, size collapsed)
          → PAPER FILL              (size>0, Alpaca paper)
          → MICRO LIVE FILL         (size>0, capped $5 real)
          → NORMAL LIVE FILL        (size>0, full)

    OBSERVATION RECEIPTS are synthetic — no broker, no money — but
    they ARE graded against future market price. They accumulate
    real expectancy, win rate, MAE/MFE, calibration, and confidence
    accuracy WITHOUT capital risk. Once a brain × lane accumulates
    100 graded observations, it unlocks the next ladder rung.

    Provenance is honest:
        receipt_type = "observation_fill"
        synthetic = true
        eligible_for_learning = true
        eligible_for_live_unlock = false   (Phase 1: read-only counter)

    Phase 2 (next iteration): scheduled resolver fetches market prices
    at +1h / +4h / +1d / +5d horizons, computes outcomes, marks
    `resolved=true`. Phase 3: unlock counter promotes brain × lane up
    the ladder. Phase 4: new sizing gate reads ladder stage.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from auth import get_current_user
from db import db
from namespaces import OBSERVATION_RECEIPTS, RUNTIMES


logger = logging.getLogger(__name__)


router = APIRouter(prefix="/admin/observation-receipts",
                   tags=["observation-receipts"])


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# Min confidence to even bother grading. Below this the brain is
# essentially noise — observation grading would pollute calibration.
OBSERVATION_MIN_CONFIDENCE = 0.30


def is_observation_candidate(intent: dict) -> tuple[bool, str]:
    """Decide whether an intent should produce an OBSERVATION RECEIPT
    instead of being silently classified as `advisory_only`.

    Conditions (ALL must be true):
        * `action` is directional (BUY, SELL, SHORT, COVER)
        * confidence ≥ OBSERVATION_MIN_CONFIDENCE
        * brain self-zeroed: size_multiplier == 0 OR
                             would_trade_without_gates == false OR
                             evidence.size_multiplier == 0
        * lane is set
        * symbol is set

    Returns (eligible: bool, reason: str).
    """
    action = (intent.get("action") or "").upper()
    if action not in {"BUY", "SELL", "SHORT", "COVER"}:
        return False, "not_directional"

    confidence = float(intent.get("confidence") or 0.0)
    if confidence < OBSERVATION_MIN_CONFIDENCE:
        return False, f"confidence_below_floor:{confidence:.3f}<{OBSERVATION_MIN_CONFIDENCE}"

    if not (intent.get("lane") and intent.get("symbol")):
        return False, "missing_lane_or_symbol"

    # Self-zero signal can live in two places: top-level evidence dict
    # (brain telemetry) or the structured size_multiplier field if the
    # brain elevates it. Either signal counts.
    evidence = intent.get("evidence") or {}
    size_mult = evidence.get("size_multiplier")
    would_trade = evidence.get("would_trade_without_gates")

    self_zeroed = (
        size_mult == 0
        or (size_mult is not None and float(size_mult) <= 0.001)
        or would_trade is False
    )
    if not self_zeroed:
        return False, "brain_sized_above_zero"

    return True, "honest_hold_eligible_for_grading"


def build_observation_receipt(intent: dict) -> dict:
    """Construct the persisted observation receipt. Synthetic — no
    broker round-trip, no fill price. The resolver job (Phase 2)
    will fill in `resolved_at`, `mark_prices`, `outcome`, etc."""
    evidence = intent.get("evidence") or {}
    snapshot = intent.get("snapshot") or {}
    # Anchor price at observation time so the resolver has a baseline.
    # Prefer the snapshot's mid; fall back to evidence/raw.
    anchor_price = (
        snapshot.get("price")
        or snapshot.get("mid")
        or evidence.get("price")
    )
    return {
        # ── identity ──────────────────────────────────────────────
        "receipt_type": "observation_fill",
        "synthetic": True,
        "eligible_for_learning": True,
        # Phase 1: read-only counter. The unlock workflow lands in
        # Phase 3 (per PRD ladder spec).
        "eligible_for_live_unlock": False,

        # ── intent provenance ─────────────────────────────────────
        "intent_id": intent.get("intent_id"),
        "brain": intent.get("stack"),
        "lane": intent.get("lane"),
        "symbol": intent.get("symbol"),
        "side": (intent.get("action") or "").upper(),
        "confidence": float(intent.get("confidence") or 0.0),

        # ── brain honesty telemetry (carried forward for analysis) ──
        "raw_confidence": evidence.get("raw_confidence"),
        "size_multiplier": evidence.get("size_multiplier"),
        "would_trade_without_gates":
            evidence.get("would_trade_without_gates"),
        "conviction_tier": evidence.get("conviction_tier"),
        "direction_label": evidence.get("direction"),

        # ── market context at observation time ─────────────────────
        "anchor_price": anchor_price,
        "anchor_snapshot": dict(snapshot),

        # ── resolver lifecycle (Phase 2 will populate) ─────────────
        "resolved": False,
        "resolved_at": None,
        "horizon_prices": {},       # e.g. {"1h": 661.2, "4h": 663.0, ...}
        "outcome": None,            # "win" | "loss" | "neutral"
        "pnl_pct": None,            # market move from anchor in % terms
        "mae_pct": None,            # max adverse excursion
        "mfe_pct": None,            # max favorable excursion

        # ── audit ─────────────────────────────────────────────────
        "created_at": _now_iso(),
    }


async def maybe_write_observation_receipt(intent: dict) -> Optional[dict]:
    """Called from `auto_router._route_one` for `advisory_only` intents.

    If the intent qualifies as an honest-hold observation, persist a
    graded-learning receipt and return it. Otherwise return None and
    the caller proceeds with the existing `advisory_only` path.
    """
    eligible, reason = is_observation_candidate(intent)
    if not eligible:
        return None
    receipt = build_observation_receipt(intent)
    receipt["candidate_reason"] = reason
    try:
        await db[OBSERVATION_RECEIPTS].insert_one(dict(receipt))
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "observation receipt persist failed intent=%s err=%r",
            intent.get("intent_id"), e,
        )
        return None
    logger.info(
        "observation receipt written intent=%s brain=%s lane=%s "
        "symbol=%s side=%s conf=%.3f",
        intent.get("intent_id"), intent.get("stack"),
        intent.get("lane"), intent.get("symbol"),
        receipt["side"], receipt["confidence"],
    )
    return receipt


# ─────────────────────────── routes ───────────────────────────


@router.get("")
async def list_observation_receipts(
    brain: Optional[str] = Query(default=None),
    lane: Optional[str] = Query(default=None),
    resolved: Optional[bool] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    """List observation receipts. Default newest-first. Useful for
    operator visibility into the learning queue."""
    q: dict = {}
    if brain:
        if brain not in RUNTIMES:
            raise HTTPException(status_code=400, detail=f"unknown brain {brain!r}")
        q["brain"] = brain
    if lane:
        if lane not in {"equity", "crypto"}:
            raise HTTPException(status_code=400, detail=f"unknown lane {lane!r}")
        q["lane"] = lane
    if resolved is not None:
        q["resolved"] = resolved
    rows = (
        await db[OBSERVATION_RECEIPTS]
        .find(q, {"_id": 0})
        .sort("created_at", -1)
        .to_list(limit)
    )
    return {"count": len(rows), "items": rows}


@router.get("/counts")
async def observation_counts(
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    """Per-brain × lane counts of graded observations. The Phase 3
    unlock counter will consume this — a brain × lane reaching 100
    resolved observations unlocks the next ladder rung.

    Doctrine pin: the 100-count threshold is a Phase 3 concern. This
    endpoint just surfaces the running tallies for operator visibility
    today; the actual unlock action lives in a later iteration."""
    pipeline = [
        {"$group": {
            "_id": {"brain": "$brain", "lane": "$lane"},
            "total": {"$sum": 1},
            "resolved": {"$sum": {"$cond": ["$resolved", 1, 0]}},
            "wins": {"$sum": {"$cond": [{"$eq": ["$outcome", "win"]}, 1, 0]}},
            "losses": {"$sum": {"$cond": [{"$eq": ["$outcome", "loss"]}, 1, 0]}},
        }},
    ]
    rows = await db[OBSERVATION_RECEIPTS].aggregate(pipeline).to_list(100)
    items = [
        {
            "brain": r["_id"]["brain"],
            "lane": r["_id"]["lane"],
            "total": r["total"],
            "resolved": r["resolved"],
            "unresolved": r["total"] - r["resolved"],
            "wins": r["wins"],
            "losses": r["losses"],
            "win_rate": (r["wins"] / r["resolved"]) if r["resolved"] else None,
            "ladder_unlock_threshold": 100,
            "progress_to_next_rung_pct": min(
                100.0, round(r["resolved"] / 100.0 * 100.0, 1),
            ) if r["resolved"] is not None else 0.0,
        }
        for r in rows
    ]
    items.sort(key=lambda x: (x["brain"], x["lane"]))
    return {
        "items": items,
        "doctrine_note": (
            "Observation receipts grade brain conviction WITHOUT capital "
            "risk. 100 resolved observations per brain × lane unlocks "
            "micro-paper trading (Phase 3, future). Today this endpoint "
            "only reports counts — unlock action is operator-gated."
        ),
        "checked_at": _now_iso(),
    }
