"""Verified Reinforcement Layer (VRL).

Doctrine (2026-02-16):
    VRL is MC's *evidence pipeline* for trade governance — it converts
    raw receipts, gate-decision logs, and outcome rows into two
    operator-facing artifacts:

    (1) PER-RECEIPT VERIFICATIONS
        For each execution receipt, capture how faithfully the broker
        honored the intent — slippage (filled price vs. intent
        reference), notional drift, fill quality. The verification is
        immutable; written once per receipt. Stored at
        SHARED_VRL_VERIFICATIONS.

    (2) PER-GATE SCORECARDS
        For each gate in the gate chain (executor_seat_check, council,
        exposure caps, …) we accumulate a confusion matrix over a
        rolling window:
            TP — gate FAILED, the underlying trade would have lost.
            FP — gate FAILED, the underlying trade would have won.
            TN — gate PASSED, the trade won (or was a scratch).
            FN — gate PASSED, the trade lost.
        Each scorecard row is one (gate_name, window_end) tuple at
        SHARED_VRL_SCORECARDS. The aggregator pulls SHARED_GATE_RESULTS
        joined with SHARED_OUTCOMES on intent_id.

    All writes are append-only. Gate scorecards are recomputed on demand
    via POST /api/admin/vrl/scorecards/recompute or — once we wire it —
    on a nightly cron.

    What VRL is NOT:
      * Not a gate. It does not block trades.
      * Not a brain. It does not generate intents or stances.
      * Not authoritative on P&L — it reads from SHARED_OUTCOMES, which
        the operator/Chevelle resolve elsewhere.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from auth import get_current_user
from db import db
from namespaces import (
    EXECUTION_RECEIPTS,
    SHARED_GATE_RESULTS,
    SHARED_INTENTS,
    SHARED_OUTCOMES,
    SHARED_VRL_SCORECARDS,
    SHARED_VRL_VERIFICATIONS,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_float(v) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


# ──────────────────────── 1. per-receipt verifications ────────────────────────

async def verify_receipt(receipt: dict, intent: Optional[dict] = None) -> dict:
    """Build (or fetch) the verification row for a single execution
    receipt. Idempotent on receipt_id — re-running returns the existing
    row without recomputation.

    Slippage and drift metrics are best-effort. If the receipt lacks
    `filled_avg_price` (e.g. the order is still queued), the row is
    written with status='pending' so a later sweep can complete it.
    """
    receipt_id = receipt.get("receipt_id")
    if not receipt_id:
        raise HTTPException(status_code=400, detail="receipt missing receipt_id")
    existing = await db[SHARED_VRL_VERIFICATIONS].find_one(
        {"receipt_id": receipt_id}, {"_id": 0},
    )
    if existing:
        return existing

    if intent is None:
        intent = await db[SHARED_INTENTS].find_one(
            {"intent_id": receipt.get("intent_id")}, {"_id": 0},
        ) or {}

    intent_ref_price = _to_float(
        (intent.get("evidence") or {}).get("price")
        or (intent.get("evidence") or {}).get("ref_price")
    )
    filled_price = _to_float(receipt.get("filled_avg_price"))
    notional = _to_float(receipt.get("notional_usd"))
    filled_qty = _to_float(receipt.get("filled_qty"))

    slippage_abs: Optional[float] = None
    slippage_pct: Optional[float] = None
    if filled_price is not None and intent_ref_price is not None and intent_ref_price > 0:
        # Direction-aware slippage. For BUY/COVER (paying), higher fill
        # price = worse. For SELL/SHORT (collecting), lower fill = worse.
        action = receipt.get("action") or ""
        if action in ("BUY", "COVER"):
            slippage_abs = filled_price - intent_ref_price
        else:
            slippage_abs = intent_ref_price - filled_price
        slippage_pct = (slippage_abs / intent_ref_price) * 100.0

    notional_realized = None
    if filled_price is not None and filled_qty is not None:
        notional_realized = filled_price * filled_qty
    notional_drift_pct: Optional[float] = None
    if notional is not None and notional_realized is not None and notional > 0:
        notional_drift_pct = ((notional_realized - notional) / notional) * 100.0

    status = "pending"
    if filled_price is not None:
        status = "verified"

    row = {
        "verification_id": str(uuid.uuid4()),
        "receipt_id": receipt_id,
        "intent_id": receipt.get("intent_id"),
        "stack": receipt.get("stack"),
        "symbol": receipt.get("symbol"),
        "lane": receipt.get("lane"),
        "action": receipt.get("action"),
        "broker_order_id": receipt.get("broker_order_id"),
        "broker": receipt.get("broker"),
        "status": status,
        "intent_ref_price": intent_ref_price,
        "filled_avg_price": filled_price,
        "filled_qty": filled_qty,
        "requested_notional_usd": notional,
        "realized_notional_usd": notional_realized,
        "slippage_abs": slippage_abs,
        "slippage_pct": slippage_pct,
        "notional_drift_pct": notional_drift_pct,
        "verified_at": _now_iso(),
    }
    await db[SHARED_VRL_VERIFICATIONS].insert_one(row.copy())
    return {k: v for k, v in row.items() if k != "_id"}


# ──────────────────────── 2. per-gate scorecards ────────────────────────

def _is_loser(outcome: dict) -> bool:
    """Truth label: did the underlying trade lose money?
    Reads pnl_usd first, then falls back to outcome_label. Returns True
    when we KNOW the trade lost; False otherwise (wins, scratches, and
    unknowns)."""
    pnl = outcome.get("pnl_usd")
    if isinstance(pnl, (int, float)):
        return pnl < 0
    label = (outcome.get("outcome_label") or outcome.get("label") or "").lower()
    return label in ("loss", "stopped_out")


async def recompute_scorecards(
    *,
    window_hours: int = 720,           # 30 days
    actor: str = "system",
) -> dict:
    """Recompute the gate scorecard table over the past `window_hours`.
    One row per gate, written to SHARED_VRL_SCORECARDS with a fresh
    `window_end` so historical recomputes accumulate (the operator can
    audit how each gate's quality drifted over time).

    Joins:
        SHARED_GATE_RESULTS (per-gate verdicts on dry-runs + submits)
        × SHARED_OUTCOMES (canonical win/loss labels by intent_id)

    For every gate we tally:
        TP  gate FAILED, trade would have lost (gate saved a loss)
        FP  gate FAILED, trade would have won  (gate cost a win)
        TN  gate PASSED, trade won/scratched   (gate correctly allowed)
        FN  gate PASSED, trade lost            (gate missed a loss)
    """
    since = (datetime.now(timezone.utc) - timedelta(hours=window_hours)).isoformat()
    now = _now_iso()

    # Pull outcomes within the window, keyed by intent_id. We use the
    # newest row per intent_id when there are duplicates.
    outcome_rows = await db[SHARED_OUTCOMES].find(
        {"resolved_at": {"$gte": since}},
        {"_id": 0, "intent_id": 1, "pnl_usd": 1, "outcome_label": 1, "label": 1, "resolved_at": 1},
    ).sort("resolved_at", -1).to_list(20000)
    outcome_by_intent: dict = {}
    for r in outcome_rows:
        iid = r.get("intent_id")
        if iid and iid not in outcome_by_intent:
            outcome_by_intent[iid] = r
    if not outcome_by_intent:
        return {
            "ok": True,
            "scorecards": [],
            "window_hours": window_hours,
            "since": since,
            "as_of": now,
            "intents_scored": 0,
            "note": "no resolved outcomes in window",
        }

    # Pull every gate-result row that maps to one of those intents.
    intent_ids = list(outcome_by_intent.keys())
    gate_rows = await db[SHARED_GATE_RESULTS].find(
        {"intent_id": {"$in": intent_ids}},
        {"_id": 0, "intent_id": 1, "kind": 1, "verdict": 1, "gates": 1, "ts": 1},
    ).to_list(50000)

    # Bucket per gate name → confusion matrix.
    # Use the gates list inside each result row when present (dry_run and
    # submit_* kinds both expose this), otherwise skip.
    tally: dict[str, dict] = {}
    for gr in gate_rows:
        gates = gr.get("gates") or []
        outcome = outcome_by_intent.get(gr.get("intent_id"))
        if outcome is None:
            continue
        loser = _is_loser(outcome)
        for g in gates:
            name = g.get("name")
            if not name:
                continue
            slot = tally.setdefault(name, {"tp": 0, "fp": 0, "tn": 0, "fn": 0})
            passed = bool(g.get("passed"))
            if not passed and loser:
                slot["tp"] += 1
            elif not passed and not loser:
                slot["fp"] += 1
            elif passed and not loser:
                slot["tn"] += 1
            elif passed and loser:
                slot["fn"] += 1

    scorecards = []
    for name, slot in sorted(tally.items()):
        tp, fp, tn, fn = slot["tp"], slot["fp"], slot["tn"], slot["fn"]
        total = tp + fp + tn + fn
        precision = tp / (tp + fp) if (tp + fp) else None
        recall = tp / (tp + fn) if (tp + fn) else None
        accuracy = (tp + tn) / total if total else None
        # "Net protect rate": of trades the gate blocked, what fraction
        # would have lost? The signature operator KPI.
        net_protect = precision  # alias for clarity in the UI
        row = {
            "scorecard_id": str(uuid.uuid4()),
            "gate_name": name,
            "window_hours": window_hours,
            "window_since": since,
            "window_end": now,
            "tp": tp, "fp": fp, "tn": tn, "fn": fn,
            "total": total,
            "precision": precision,
            "recall": recall,
            "accuracy": accuracy,
            "net_protect_rate": net_protect,
            "computed_by": actor,
        }
        scorecards.append(row)

    if scorecards:
        await db[SHARED_VRL_SCORECARDS].insert_many([dict(r) for r in scorecards])

    return {
        "ok": True,
        "scorecards": scorecards,
        "window_hours": window_hours,
        "since": since,
        "as_of": now,
        "intents_scored": len(outcome_by_intent),
        "gate_rows_seen": len(gate_rows),
    }


# ──────────────────────── REST surface ────────────────────────

router = APIRouter(prefix="/admin/vrl", tags=["vrl"])


@router.get("/verifications")
async def list_verifications(
    receipt_id: Optional[str] = Query(default=None),
    intent_id: Optional[str] = Query(default=None),
    stack: Optional[str] = Query(default=None),
    status: Optional[str] = Query(default=None, description="pending | verified"),
    limit: int = Query(default=100, ge=1, le=500),
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    q: dict = {}
    if receipt_id:
        q["receipt_id"] = receipt_id
    if intent_id:
        q["intent_id"] = intent_id
    if stack:
        q["stack"] = stack
    if status:
        q["status"] = status
    rows = await db[SHARED_VRL_VERIFICATIONS].find(q, {"_id": 0}) \
        .sort("verified_at", -1).to_list(limit)
    return {"items": rows, "count": len(rows)}


class VerifyByReceiptIn(BaseModel):
    receipt_id: str = Field(..., min_length=8, max_length=80)


@router.post("/verify")
async def verify_endpoint(
    body: VerifyByReceiptIn,
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    """Force a verification for a specific receipt. Idempotent — returns
    the cached row if it already exists. Use this from the operator UI
    when the auto-verification on /execution/submit was skipped or
    incomplete."""
    receipt = await db[EXECUTION_RECEIPTS].find_one(
        {"receipt_id": body.receipt_id}, {"_id": 0},
    )
    if not receipt:
        raise HTTPException(status_code=404, detail=f"receipt {body.receipt_id} not found")
    return await verify_receipt(receipt)


@router.get("/scorecards")
async def list_scorecards(
    gate_name: Optional[str] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    latest_only: bool = Query(
        default=True,
        description="when true, returns only the freshest row per gate",
    ),
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    q: dict = {}
    if gate_name:
        q["gate_name"] = gate_name
    rows = await db[SHARED_VRL_SCORECARDS].find(q, {"_id": 0}) \
        .sort("window_end", -1).to_list(limit)
    if latest_only and rows:
        seen: set[str] = set()
        dedup = []
        for r in rows:
            name = r.get("gate_name") or ""
            if name in seen:
                continue
            seen.add(name)
            dedup.append(r)
        rows = dedup
    return {"items": rows, "count": len(rows)}


class RecomputeIn(BaseModel):
    window_hours: int = Field(default=720, ge=1, le=24 * 365)


@router.post("/scorecards/recompute")
async def recompute_endpoint(
    body: RecomputeIn,
    user: dict = Depends(get_current_user),  # noqa: B008
):
    return await recompute_scorecards(
        window_hours=body.window_hours,
        actor=user.get("email") or "operator",
    )
