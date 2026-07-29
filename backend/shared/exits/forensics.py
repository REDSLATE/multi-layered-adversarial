"""Loss forensics — autopsies for realized losses (2026-07-28).

Operator doctrine: "RISEDUAL cannot improve if the post-mortem
pipeline has no complete trade records to grade." Every EXCEPTIONAL
loss (>2R, or unknown-risk losses > $20) files an automatic forensic
report; the operator can also pull an on-demand report for every
realized loss above a dollar threshold.

An autopsy answers, per loss:
  * originating brain, lane, symbol, regime
  * entry / stop / exit and exit trigger
  * Governor multiplier + risk budget vs projected & realized loss
  * RoadGuard / risk-gate verdict at entry
  * market-data completeness at intent time (enrichment_status,
    bars_used, missing fields)
  * every attribution link that is MISSING — the report never hides
    a broken chain, it names it.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from db import db

logger = logging.getLogger("risedual.exit_forensics")

FORENSICS = "shared_forensic_reports"
EXIT_OUTCOMES = "shared_exit_outcomes"

_INTENT_PROJ = {
    "_id": 0, "intent_id": 1, "stack": 1, "confidence": 1,
    "gate_state": 1, "risk_sizing": 1, "snapshot": 1,
    "doctrine_packet.base_labels.quality": 1, "regime": 1,
    "target_price": 1, "stop_price": 1, "ingest_ts": 1,
}
_EXEC_PROJ = {
    "_id": 0, "intent_id": 1, "ts": 1, "broker": 1, "notional_usd": 1,
    "risk_ok": 1, "risk_reason": 1, "seat_holder": 1, "seats": 1,
    "broker_status": 1,
}


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _f(v: Any) -> Optional[float]:
    try:
        out = float(v)
        return out if out == out else None
    except (TypeError, ValueError):
        return None


async def build_autopsy(outcome: dict) -> dict:
    """Join the outcome row back to its origin intent + entry
    execution and produce the full autopsy. Read-only; fail-soft on
    every join — a missing link becomes an `attribution_gaps` entry,
    never an exception."""
    trade_id = outcome.get("trade_id") or outcome.get("origin_intent_id")
    gaps: list[str] = []

    intent: Optional[dict] = None
    execution: Optional[dict] = None
    if trade_id:
        try:
            intent = await db["shared_intents"].find_one(
                {"intent_id": trade_id}, _INTENT_PROJ,
            )
        except Exception:  # noqa: BLE001
            pass
        try:
            execution = await db["executions"].find_one(
                {"intent_id": trade_id, "ok": True}, _EXEC_PROJ,
                max_time_ms=4000,
            )
        except Exception:  # noqa: BLE001
            pass
    else:
        gaps.append("no_trade_id_on_outcome")

    if outcome.get("attribution") != "trade_id":
        gaps.append(f"attribution_mode={outcome.get('attribution') or 'unknown'}")
    if not outcome.get("brain"):
        gaps.append("no_brain_attribution")
    if not _f(outcome.get("initial_risk")):
        gaps.append("no_initial_risk_r_multiple_not_computable")
    if trade_id and intent is None:
        gaps.append("origin_intent_missing_from_shared_intents")
    if trade_id and execution is None:
        gaps.append("entry_execution_missing_from_executions")
    if not _f(outcome.get("exit_price")):
        gaps.append("no_exit_price_on_outcome")

    rs = (intent or {}).get("risk_sizing") or {}
    snap = (intent or {}).get("snapshot") or {}
    net_pnl = _f(outcome.get("net_pnl"))
    risk_budget = _f(rs.get("risk_budget")) or _f(outcome.get("initial_risk"))
    projected_loss = _f(rs.get("projected_loss_at_stop"))
    overshoot_x = None
    if net_pnl is not None and net_pnl < 0 and risk_budget and risk_budget > 0:
        overshoot_x = round(abs(net_pnl) / risk_budget, 3)

    return {
        "trade_id": trade_id,
        "plan_id": outcome.get("plan_id"),
        "brain": outcome.get("brain") or (intent or {}).get("stack"),
        "lane": outcome.get("lane"),
        "symbol": outcome.get("symbol"),
        "regime": outcome.get("regime") or (intent or {}).get("regime"),
        "side": outcome.get("side"),
        # economics
        "entry_price": outcome.get("entry_price"),
        "stop_price": outcome.get("stop_price") or rs.get("stop_price"),
        "target_price": outcome.get("target_price") or rs.get("target_price"),
        "exit_price": outcome.get("exit_price"),
        "exit_reason": outcome.get("exit_reason"),
        "outcome": outcome.get("outcome"),
        "qty": outcome.get("qty"),
        "gross_pnl": outcome.get("gross_pnl"),
        "fees_est": outcome.get("fees_est"),
        "net_pnl": net_pnl,
        "initial_risk": _f(outcome.get("initial_risk")),
        "realized_r_multiple": outcome.get("realized_r_multiple"),
        "loss_escalation": outcome.get("loss_escalation"),
        "levels_source": outcome.get("levels_source"),
        # governor / risk at entry
        "governor_multiplier": rs.get("governor_multiplier"),
        "risk_budget": risk_budget,
        "projected_loss_at_stop": projected_loss,
        "loss_overshoot_x_budget": overshoot_x,
        "entry_notional": _f(rs.get("final_notional"))
                          or _f((execution or {}).get("notional_usd")),
        # roadguard / seat at entry
        "roadguard": {
            "risk_ok": (execution or {}).get("risk_ok"),
            "risk_reason": (execution or {}).get("risk_reason"),
        } if execution else None,
        "seat_holder": (execution or {}).get("seat_holder")
                       or outcome.get("seat_role"),
        "broker": (execution or {}).get("broker"),
        # market-data completeness at intent time
        "data_completeness": {
            "enrichment_status": snap.get("enrichment_status"),
            "bars_used": snap.get("bars_used"),
            "missing_required_fields": snap.get("missing_required_fields"),
            "spread_source": snap.get("spread_source"),
            "snapshot_source": snap.get("snapshot_source"),
        } if intent else None,
        "doctrine_quality": (((intent or {}).get("doctrine_packet") or {})
                             .get("base_labels") or {}).get("quality"),
        "confidence_at_intent": (intent or {}).get("confidence"),
        "attribution": outcome.get("attribution"),
        "attribution_gaps": gaps,
        "attribution_complete": not gaps,
        "adopted_at": outcome.get("adopted_at"),
        "closed_at": outcome.get("closed_at"),
    }


async def file_forensic_report(
    outcome: dict, kind: str = "auto_exceptional_loss",
) -> Optional[dict]:
    """Persist one forensic report per (plan_id, kind). Idempotent —
    outbox replay safe. Called automatically on EXCEPTIONAL_LOSS."""
    try:
        plan_id = outcome.get("plan_id")
        existing = await db[FORENSICS].find_one(
            {"plan_id": plan_id, "kind": kind}, {"_id": 0}, max_time_ms=4000,
        )
        if existing:
            return existing
        autopsy = await build_autopsy(outcome)
        doc = {
            "report_id": uuid.uuid4().hex,
            "kind": kind,
            "plan_id": plan_id,
            "trade_id": autopsy.get("trade_id"),
            "lane": autopsy.get("lane"),
            "symbol": autopsy.get("symbol"),
            "brain": autopsy.get("brain"),
            "net_pnl": autopsy.get("net_pnl"),
            "realized_r_multiple": autopsy.get("realized_r_multiple"),
            "filed_at": _iso(),
            "autopsy": autopsy,
        }
        await db[FORENSICS].insert_one(dict(doc))
        logger.warning(
            "FORENSIC REPORT FILED kind=%s %s %s brain=%s net_pnl=%s r=%s gaps=%s",
            kind, autopsy.get("lane"), autopsy.get("symbol"),
            autopsy.get("brain"), autopsy.get("net_pnl"),
            autopsy.get("realized_r_multiple"), autopsy.get("attribution_gaps"),
        )
        return doc
    except Exception as exc:  # noqa: BLE001
        logger.warning("forensic report filing failed: %s", exc)
        return None


async def large_loss_report(
    min_loss_usd: float = 20.0, days: int = 30,
) -> dict:
    """On-demand autopsy for every realized loss ≥ `min_loss_usd`
    (net when known, gross fallback) in the last `days` days."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    q = {
        "closed_at": {"$gte": since},
        "$or": [
            {"net_pnl": {"$lte": -abs(min_loss_usd)}},
            {"net_pnl": None, "realized_pnl_usd": {"$lte": -abs(min_loss_usd)}},
        ],
    }
    rows = await db[EXIT_OUTCOMES].find(q, {"_id": 0}).sort(
        "closed_at", -1,
    ).max_time_ms(8000).limit(200).to_list(200)

    autopsies = [await build_autopsy(r) for r in rows]
    by_brain: dict[str, dict] = {}
    total_loss = 0.0
    gap_count = 0
    for a in autopsies:
        loss = _f(a.get("net_pnl")) or 0.0
        total_loss += loss
        if a["attribution_gaps"]:
            gap_count += 1
        key = a.get("brain") or "unattributed"
        agg = by_brain.setdefault(key, {"count": 0, "net_pnl": 0.0})
        agg["count"] += 1
        agg["net_pnl"] = round(agg["net_pnl"] + loss, 4)
    return {
        "min_loss_usd": min_loss_usd,
        "days": days,
        "losses": len(autopsies),
        "total_net_pnl": round(total_loss, 4),
        "with_attribution_gaps": gap_count,
        "by_brain": by_brain,
        "reports": autopsies,
        "generated_at": _iso(),
    }
