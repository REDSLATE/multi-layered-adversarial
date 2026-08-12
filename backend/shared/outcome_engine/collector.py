"""Outcome collector — feeds the RISE outcome engine from live data.

Runs OUTSIDE the hot path on its own loop. Every cycle it finds
recently expired signals in `shared_intents` (BUY entries whose
max-holding window has fully elapsed), rebuilds the tape they saw from
`shared_ohlcv_bars`, runs triple-barrier + attribution, and persists
one record per signal to the SQLite hot store (Mongo mirror in
`rise_signal_outcomes` for dashboards).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from db import db
from shared.outcome_engine import store
from shared.outcome_engine.engine import (
    ExecutionAttributionEngine, ExecutionSnapshot, PricePoint,
    Side, SignalSnapshot, TripleBarrierEngine,
)

logger = logging.getLogger("risedual.outcome_collector")

INTERVAL_SEC = float(os.environ.get("OUTCOME_COLLECTOR_INTERVAL_SEC", "300"))
BATCH = int(os.environ.get("OUTCOME_COLLECTOR_BATCH", "50"))
DEFAULT_TP_PCT = 0.06
DEFAULT_SL_PCT = 0.03
DEFAULT_HOLD_S = 24 * 3600

_state: dict[str, Any] = {"running": False, "task": None, "last_run": None,
                          "resolved_total": 0, "errors": 0,
                          "resolved_last_cycle": 0, "hydrated_on_boot": 0,
                          "exit_linkage_miss_count": 0}


def _candidate_filter(lookback_iso: str) -> dict:
    return {"action": {"$in": ["BUY", "SHORT"]},
            "signal_price": {"$gt": 0},
            "signal_detected_at": {"$gte": lookback_iso},
            "outcome_resolved": {"$ne": True},
            "$or": [{"executed": True},
                    {"blocked_by.0": {"$exists": True}},
                    {"would_have_traded_without_gates": True},
                    {"broker_reason": {"$nin": [None, ""]}}]}


def _dt(v) -> Optional[datetime]:
    if not v:
        return None
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


async def _lane_defaults(lane: str) -> tuple[float, float, int]:
    try:
        from shared.exits.policy import get_policy  # noqa: WPS433
        pol = (await get_policy()).get(lane) or {}
        return (float(pol.get("tp_pct", DEFAULT_TP_PCT * 100)) / 100,
                float(pol.get("sl_pct", DEFAULT_SL_PCT * 100)) / 100,
                int(float(pol.get("max_hold_h", 24)) * 3600))
    except Exception:  # noqa: BLE001
        return DEFAULT_TP_PCT, DEFAULT_SL_PCT, DEFAULT_HOLD_S


async def _price_points(symbol: str, start: datetime,
                        end: datetime) -> list[PricePoint]:
    """Rebuild the tape: per 5m/1m bar emit LOW then HIGH so the
    conservative stop-first ordering inside a bar is preserved."""
    points: list[PricePoint] = []
    for tf in ("1m", "5m", "1d"):
        rows = await db["shared_ohlcv_bars"].find(
            {"symbol": symbol, "tf": tf,
             "ts": {"$gte": start.isoformat(), "$lte": end.isoformat()}},
            {"_id": 0, "ts": 1, "h": 1, "l": 1, "c": 1},
        ).sort("ts", 1).max_time_ms(8000).to_list(2000)
        if len(rows) >= 2:
            for r in rows:
                ts = _dt(r["ts"])
                if not ts:
                    continue
                lo, hi = float(r.get("l") or 0), float(r.get("h") or 0)
                if lo > 0:
                    points.append(PricePoint(timestamp=ts, price=lo))
                if hi > 0:
                    points.append(PricePoint(timestamp=ts, price=hi))
            return points
    return points


def _build_signal(intent: dict, tp: float, sl: float,
                  hold_s: int) -> Optional[SignalSnapshot]:
    price = intent.get("signal_price")
    ts = _dt(intent.get("signal_detected_at") or intent.get("ingest_ts"))
    if not price or float(price) <= 0 or not ts:
        return None
    # brain-authored levels beat lane defaults (never unbounded)
    target, stop = intent.get("target_price"), intent.get("stop_price")
    p = float(price)
    if target and float(target) > 0:
        tp = abs(float(target) / p - 1)
    if stop and float(stop) > 0:
        sl = abs(1 - float(stop) / p)
    return SignalSnapshot(
        signal_id=intent.get("intent_id") or str(intent.get("_id")),
        symbol=intent.get("symbol") or "?",
        lane=intent.get("lane") or "?",
        brain=(intent.get("brain") or intent.get("stack")
               or intent.get("strategy_id") or "unknown"),
        side=Side.BUY if (intent.get("action") or "").upper() == "BUY" else Side.SELL,
        signal_time=ts, signal_price=p,
        confidence=float(intent.get("confidence") or 0),
        profit_target_pct=tp, stop_loss_pct=sl,
        max_holding_seconds=hold_s,
        regime=intent.get("regime"),
    )


async def _build_execution(intent: dict) -> ExecutionSnapshot:
    sid = intent.get("intent_id") or str(intent.get("_id"))
    executed = bool(intent.get("executed"))
    entry_time = _dt(intent.get("executed_at"))
    entry_price = None
    exit_time = exit_price = None
    if executed:
        exe = intent.get("execution") or {}
        entry_price = exe.get("fill_price") or exe.get("price")
        row = await db["executions"].find_one(
            {"intent_id": sid, "ok": True}, {"broker_response": 1, "ts": 1})
        if row and not entry_price:
            br = row.get("broker_response") or {}
            entry_price = br.get("fill_price") or br.get("price") or br.get("avg_price")
        if row and not entry_time:
            entry_time = _dt(row.get("ts"))
        outcome = await db["shared_exit_outcomes"].find_one(
            {"$or": [{"trade_id": sid}, {"origin_intent_id": sid}]},
            {"exit_price": 1, "closed_at": 1})
        if outcome is None:
            # schema drift OR position never closed — both must be visible
            _state["exit_linkage_miss_count"] += 1
        if outcome:
            exit_price = outcome.get("exit_price")
            exit_time = _dt(outcome.get("closed_at"))
    reasons = intent.get("blocked_by") or []
    gate_reason = (", ".join(map(str, reasons)) if reasons
                   else intent.get("broker_reason") if not executed else None)
    return ExecutionSnapshot(
        signal_id=sid, executed=executed,
        entry_time=entry_time,
        entry_price=float(entry_price) if entry_price else None,
        exit_time=exit_time,
        exit_price=float(exit_price) if exit_price else None,
        gate_rejection_reason=gate_reason,
    )


async def resolve_batch(limit: int = BATCH) -> dict:
    """Resolve signals whose holding window has fully elapsed.

    Only intents that MATTERED are scored (executed, gate-blocked, or
    would-have-traded) — scoring every one of prod's ~113k daily raw
    intents is neither feasible nor informative. Processed intents are
    stamped `outcome_resolved` in Mongo so the batch window always
    advances instead of re-fetching the same oldest 500 forever."""
    now = datetime.now(timezone.utc)
    attr_engine = ExecutionAttributionEngine()
    lookback = (now - timedelta(days=7)).isoformat()
    candidates = await db["shared_intents"].find(
        _candidate_filter(lookback),
        {"doctrine_packet": 0, "evidence": 0, "snapshot": 0, "weights": 0,
         "spread_enrichment_diagnostics": 0},
    ).sort("signal_detected_at", 1).limit(500).to_list(500)

    async def _mark_resolved(intent_doc: dict) -> None:
        await db["shared_intents"].update_one(
            {"_id": intent_doc["_id"]}, {"$set": {"outcome_resolved": True}})

    resolved = skipped = 0
    for intent in candidates:
        sid = intent.get("intent_id") or str(intent.get("_id"))
        if store.has_signal(sid):
            await _mark_resolved(intent)  # self-heal missing stamp
            continue
        tp, sl, hold_s = await _lane_defaults(intent.get("lane") or "crypto")
        signal = _build_signal(intent, tp, sl, hold_s)
        if signal is None:
            skipped += 1
            await _mark_resolved(intent)  # unparseable — never retryable
            continue
        if (now - signal.signal_time).total_seconds() < signal.max_holding_seconds:
            continue  # window still open — resolve later
        points = await _price_points(
            signal.symbol, signal.signal_time,
            signal.signal_time + timedelta(seconds=signal.max_holding_seconds))
        theoretical = TripleBarrierEngine.evaluate(signal, points)
        execution = await _build_execution(intent)
        attribution = attr_engine.classify(signal, execution, theoretical)
        record = {
            "outcome_id": uuid.uuid4().hex,
            "signal_id": sid,
            "symbol": signal.symbol, "lane": signal.lane,
            "brain": signal.brain, "side": signal.side.value,
            "signal_time": signal.signal_time.isoformat(),
            "signal_price": signal.signal_price,
            "confidence": signal.confidence,
            "theoretical_outcome": theoretical.outcome.value,
            "theoretical_return_pct": theoretical.return_pct,
            "executed": execution.executed,
            "actual_entry_time": (execution.entry_time.isoformat()
                                  if execution.entry_time else None),
            "actual_entry_price": execution.entry_price,
            "actual_exit_time": (execution.exit_time.isoformat()
                                 if execution.exit_time else None),
            "actual_exit_price": execution.exit_price,
            "actual_return_pct": attr_engine.calculate_actual_return(
                signal, execution),
            "entry_delay_seconds": attr_engine.calculate_entry_delay(
                signal, execution),
            "entry_slippage_pct": attr_engine.calculate_entry_slippage(
                signal, execution),
            "edge_capture_ratio": attr_engine.calculate_edge_capture_ratio(
                theoretical.return_pct,
                attr_engine.calculate_actual_return(signal, execution)),
            "attribution": attribution.value,
            "gate_rejection_reason": execution.gate_rejection_reason,
            "metadata_json": json.dumps({
                k: v for k, v in (
                    ("regime_ctx", intent.get("regime_ctx")),
                    ("setup_id", intent.get("setup_id")),
                ) if v
            }),
            "created_at": now.isoformat(),
        }
        store.save(record)
        try:  # Mongo mirror for dashboards + redeploy survival
            await db["rise_signal_outcomes"].update_one(
                {"signal_id": sid}, {"$set": record}, upsert=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("outcome mongo mirror failed %s: %s", sid, exc)
        await _mark_resolved(intent)
        resolved += 1
        if resolved >= limit:
            break
    _state["last_run"] = now.isoformat()
    _state["resolved_total"] += resolved
    _state["resolved_last_cycle"] = resolved
    return {"resolved": resolved, "skipped": skipped,
            "candidates": len(candidates)}


async def _hydrate_from_mirror() -> None:
    """SQLite lives on ephemeral disk in the deploy environment —
    rebuild it from the Mongo mirror after a redeploy so rollups
    don't reset to zero."""
    if store.counts()["rows"] > 0:
        return
    rows = await db["rise_signal_outcomes"].find(
        {}, {"_id": 0}).sort("created_at", -1).to_list(20000)
    for row in rows:
        try:
            store.save(row)
        except Exception:  # noqa: BLE001
            continue
    if rows:
        _state["hydrated_on_boot"] = len(rows)
        logger.info("outcome store hydrated from mongo mirror rows=%s", len(rows))


async def _loop() -> None:
    logger.info("outcome_collector loop start interval=%.0fs", INTERVAL_SEC)
    try:
        await db["shared_intents"].create_index(
            [("outcome_resolved", 1), ("signal_detected_at", 1)],
            name="idx_outcome_resolved_signal_time", background=True)
        await _hydrate_from_mirror()
    except Exception as exc:  # noqa: BLE001
        logger.warning("outcome_collector bootstrap issue: %s", exc)
    while True:
        try:
            summary = await resolve_batch()
            if summary["resolved"]:
                logger.info("outcome_collector resolved=%s", summary["resolved"])
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            _state["errors"] += 1
            logger.exception("outcome_collector tick failed: %s", exc)
        await asyncio.sleep(INTERVAL_SEC)


def start_if_enabled() -> None:
    if (os.environ.get("OUTCOME_COLLECTOR_ENABLED") or "true").strip().lower() in (
            "0", "false", "no", "off"):
        logger.info("outcome_collector disabled via env")
        return
    if _state.get("running"):
        return
    task = asyncio.get_event_loop().create_task(_loop(), name="outcome_collector")
    _state.update(running=True, task=task)
    logger.info("outcome_collector started")


def get_status() -> dict:
    return {**{k: v for k, v in _state.items() if k != "task"},
            "store": store.counts()}


async def get_status_async() -> dict:
    """Status + the durable-queue health counter (Mongo count)."""
    status = get_status()
    lookback = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    try:
        status["eligible_unresolved"] = await db["shared_intents"].count_documents(
            _candidate_filter(lookback), maxTimeMS=8000)
    except Exception as exc:  # noqa: BLE001
        status["eligible_unresolved"] = None
        logger.warning("eligible_unresolved count failed: %s", exc)
    return status
