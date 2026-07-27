"""Realized exit outcomes → brain learning loop (2026-07-22).

Operator directive: stamp each closed plan's result (tp_hit / sl_hit
/ timeout + realized P&L) onto the originating brain's record so the
arbiter seats brains by REALIZED performance, not confidence alone.

Mechanics: the arbiter already seats by DAWE weights
(`brain_runtime_metrics.brains.{brain}.dawe.{lane}`), which the
grader feeds from 15m/60m PREDICTION grades. This module folds
realized round-trip P&L into the same `session_weight` EWMA using
identical semantics (`quality_from_signed_return` → `update_session`)
so realized outcomes and prediction grades converge in one stream.

Quality scaling: `expected_move` = the lane's take-profit band
(tp_pct/100). A full TP hit grades 1.0; a full SL hit (−sl_pct)
grades below 0.5 toward 0.0; scratch exits grade ~0.5 (neutral).

Ledger: every outcome is written to `shared_exit_outcomes`
(permanent — not in retention rules) with brain attribution.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from db import db

logger = logging.getLogger("risedual.exit_outcomes")

EXIT_OUTCOMES = "shared_exit_outcomes"

_OUTCOME_LABELS = {
    "take_profit": "tp_hit",
    "stop_loss": "sl_hit",
    "max_hold": "timeout",
    "manual_close": "manual",
}


def _label(plan: dict) -> str:
    if plan.get("close_detail") == "position_closed_externally":
        return "external"
    return _OUTCOME_LABELS.get(plan.get("exit_reason") or "", "unknown")


async def record_outcome(plan: dict) -> Optional[dict]:
    """Write the permanent outcome row and fold realized P&L into
    the originating brain's DAWE. Fail-soft — an outcome-write
    failure must never block plan closure."""
    try:
        return await _record(plan)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "exit outcome record failed plan=%s: %s",
            plan.get("plan_id"), exc,
        )
        return None


async def _record(plan: dict) -> dict:
    # Idempotency guard (2026-07-23 outbox replay safety): the outbox
    # writer may re-apply this event after a crash between apply and
    # ack. One outcome row + one DAWE fold per plan, ever.
    existing = await db[EXIT_OUTCOMES].find_one(
        {"plan_id": plan["plan_id"]}, {"_id": 0},
    )
    if existing:
        return existing

    lane = plan["lane"]
    entry = float(plan.get("entry_price") or 0)
    exit_price = plan.get("exit_price_est")
    qty = float(plan.get("qty_held") or 0)
    # Options: prices are per-share premiums, qty is contracts —
    # dollar PnL scales by the contract multiplier (pct unaffected).
    _mult = 100.0 if (plan.get("lane") == "options") else 1.0
    outcome = _label(plan)

    pnl_pct: Optional[float] = None
    pnl_usd: Optional[float] = None
    if exit_price and entry > 0:
        pnl_pct = (float(exit_price) / entry - 1.0) * 100.0
        pnl_usd = (float(exit_price) - entry) * qty * _mult

    row = {
        "plan_id": plan["plan_id"],
        "trade_id": plan.get("trade_id") or plan.get("origin_intent_id"),
        "lane": lane,
        "symbol": plan["symbol"],
        "outcome": outcome,
        "brain": plan.get("origin_stack"),
        "seat_role": plan.get("seat_role"),
        "side": plan.get("side") or "BUY",
        "regime": plan.get("regime"),
        "attribution": plan.get("attribution")
                       or ("trade_id" if plan.get("origin_intent_id") else "unmatched"),
        "origin_intent_id": plan.get("origin_intent_id"),
        "entry_price": entry or None,
        "exit_price": float(exit_price) if exit_price else None,
        "qty": qty,
        "realized_pnl_pct": pnl_pct,
        "realized_pnl_usd": pnl_usd,
        "levels_source": plan.get("levels_source"),
        "stop_price": plan.get("stop_price"),
        "target_price": plan.get("target_price"),
        "adopted_at": plan.get("adopted_at"),
        "closed_at": datetime.now(timezone.utc).isoformat(),
        "source": "EXIT_MONITOR",
        "verified": plan.get("close_detail") == "exit_order_filled",
        "dawe_folded": False,
    }

    # ── ResolvedTradeOutcome economics (2026-07-27 operator spec) ──
    # realized_r_multiple = net_pnl / initial_risk — lets a $10 trade
    # and a $1,000 trade be compared fairly.
    initial_risk = None
    try:
        initial_risk = float(plan.get("initial_risk") or 0) or None
    except (TypeError, ValueError):
        pass
    fees_est = None
    if pnl_usd is not None and entry > 0:
        _fee_frac = {"crypto": 0.006, "equity": 0.001, "options": 0.01}.get(lane, 0.0)
        fees_est = round(entry * qty * _mult * _fee_frac, 4)
    net_pnl = (pnl_usd - fees_est) if (pnl_usd is not None and fees_est is not None) else pnl_usd
    r_multiple = None
    if net_pnl is not None and initial_risk and initial_risk > 0:
        r_multiple = round(net_pnl / initial_risk, 3)
    result = None
    if net_pnl is not None:
        _eps = max(0.01, 0.001 * entry * qty * _mult)
        result = ("WIN" if net_pnl > _eps
                  else "LOSS" if net_pnl < -_eps else "BREAKEVEN")
    row.update({
        "gross_pnl": pnl_usd,
        "fees_est": fees_est,
        "net_pnl": round(net_pnl, 4) if net_pnl is not None else None,
        "initial_risk": initial_risk,
        "realized_r_multiple": r_multiple,
        "result": result,
        "exit_reason": plan.get("exit_reason"),
    })

    # ── Loss-escalation doctrine (2026-07-27) ──
    # <1R → normal expectancy update. 1–2R → LARGE_LOSS: double DAWE
    # fold (EWMA decays, so influence reduction is temporary). >2R →
    # EXCEPTIONAL_LOSS: automatic forensic report.
    escalation = "NORMAL"
    if r_multiple is not None and r_multiple < -1.0:
        escalation = "EXCEPTIONAL_LOSS" if r_multiple < -2.0 else "LARGE_LOSS"
    elif r_multiple is None and net_pnl is not None and net_pnl < -20.0:
        escalation = "EXCEPTIONAL_LOSS"   # unknown risk budget + big $ loss
    row["loss_escalation"] = escalation

    # Confluence attribution (2026-07-22 weighted-doctrine rollout):
    # lets the Expectancy Panel compare full-confluence trades vs
    # 2/3 half-size probes. Fail-soft — intent may be swept already.
    intent_id = plan.get("origin_intent_id")
    if intent_id:
        try:
            idoc = await db["shared_intents"].find_one(
                {"intent_id": intent_id}, {"evidence": 1},
            )
            ev = (idoc or {}).get("evidence") or {}
            conf = ev.get("confluence") or {}
            row["confluence_mode"] = conf.get("buy_mode")
            row["size_multiplier"] = ev.get("size_multiplier")
        except Exception:  # noqa: BLE001
            pass

    brain = plan.get("origin_stack")
    if brain and pnl_pct is not None:
        row["dawe_folded"] = await _fold_into_dawe(brain, lane, pnl_pct / 100.0)
        if escalation in ("LARGE_LOSS", "EXCEPTIONAL_LOSS"):
            # second fold = temporarily lowered influence (EWMA decays)
            await _fold_into_dawe(brain, lane, pnl_pct / 100.0)

    await db[EXIT_OUTCOMES].insert_one(dict(row))
    if escalation == "EXCEPTIONAL_LOSS":
        try:
            from shared.exits.forensics import file_forensic_report  # noqa: WPS433
            await file_forensic_report(row)
        except Exception as exc:  # noqa: BLE001
            logger.warning("forensic filing failed %s: %s", row.get("trade_id"), exc)
    logger.info(
        "exit outcome %s %s %s brain=%s pnl=%s%% dawe_folded=%s",
        lane, plan["symbol"], outcome, brain,
        f"{pnl_pct:+.2f}" if pnl_pct is not None else "?",
        row["dawe_folded"],
    )
    return row


async def _fold_into_dawe(brain: str, lane: str, realized_return: float) -> bool:
    """One `update_session` fold — same path the prediction grader
    uses, so the arbiter needs zero changes to feel it."""
    try:
        from mc_arbiter.arbiter import load_dawe, save_dawe  # noqa: WPS433
        from mc_arbiter.dawe import (  # noqa: WPS433
            quality_from_signed_return, update_session,
        )
        from shared.exits.policy import get_policy  # noqa: WPS433

        policy = await get_policy()
        expected_move = max(1e-4, policy[lane]["tp_pct"] / 100.0)
        quality = quality_from_signed_return(
            signed_return=realized_return, expected_move=expected_move,
        )
        state = await load_dawe(brain, lane)
        state.session_weight = update_session(
            prev_weight=state.session_weight, observed_quality=quality,
        )
        state.grades_used_session += 1
        await save_dawe(state)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("dawe fold failed brain=%s lane=%s: %s", brain, lane, exc)
        return False


async def brain_scorecard(limit_days: int = 30) -> list[dict]:
    """Per-brain realized performance aggregate for the operator UI."""
    from datetime import timedelta
    since = (
        datetime.now(timezone.utc) - timedelta(days=limit_days)
    ).isoformat()
    pipeline = [
        {"$match": {"closed_at": {"$gte": since}}},
        {"$group": {
            "_id": {"brain": {"$ifNull": ["$brain", "unattributed"]},
                    "lane": "$lane"},
            "closed": {"$sum": 1},
            "tp_hit": {"$sum": {"$cond": [{"$eq": ["$outcome", "tp_hit"]}, 1, 0]}},
            "sl_hit": {"$sum": {"$cond": [{"$eq": ["$outcome", "sl_hit"]}, 1, 0]}},
            "timeout": {"$sum": {"$cond": [{"$eq": ["$outcome", "timeout"]}, 1, 0]}},
            "avg_pnl_pct": {"$avg": "$realized_pnl_pct"},
            "total_pnl_usd": {"$sum": "$realized_pnl_usd"},
        }},
        {"$sort": {"total_pnl_usd": -1}},
    ]
    out = []
    async for r in db[EXIT_OUTCOMES].aggregate(pipeline):
        out.append({
            "brain": r["_id"]["brain"],
            "lane": r["_id"]["lane"],
            "closed": r["closed"],
            "tp_hit": r["tp_hit"],
            "sl_hit": r["sl_hit"],
            "timeout": r["timeout"],
            "avg_pnl_pct": round(r["avg_pnl_pct"], 3) if r.get("avg_pnl_pct") is not None else None,
            "total_pnl_usd": round(r["total_pnl_usd"], 4) if r.get("total_pnl_usd") is not None else None,
        })
    return out
