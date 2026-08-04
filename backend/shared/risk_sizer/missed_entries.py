"""Missed-Entry Ledger (2026-08-04 operator directive).

The feedback loop only learns from trades that HAPPENED. Nothing
learns from trades that were BLOCKED. For every blocked BUY (entry
timing chase blocks, eligibility rejects, cooldowns), this ledger
records the counterfactual: what would the position have done over
the next `horizon_h` hours against the lane's real exit levels.
Evidence base for tuning min_score, chase caps, and the $5 sizing —
instead of tuning blind.

Rows in `missed_entry_outcomes` are permanent (small, like
shared_exit_outcomes — deliberately NOT in retention rules).
Observe-only: this module never gates or emits anything.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

logger = logging.getLogger("risedual.missed_entries")

FLAG_ID = "missed_entry_ledger"
COLLECTION = "missed_entry_outcomes"
DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "horizon_h": 4.0,
    "max_eval_per_cycle": 25,
}

# risk_sizer:{reason} blocks worth a counterfactual — opportunity
# gates only, never mechanical invalidity (sized_to_zero etc.)
_SIZER_SCOPE = {
    "not_in_buy_allowlist", "below_volume_floor", "spread_too_wide",
    "no_volume_data", "no_quote", "denylisted",
    "eligibility_cap_below_broker_min", "post_sell_cooldown",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _f(v) -> Optional[float]:
    try:
        f = float(v)
        return f if f > 0 else None
    except (TypeError, ValueError):
        return None


async def get_config() -> dict:
    from db import db  # noqa: WPS433
    doc = await db["runtime_flags"].find_one(
        {"_id": FLAG_ID}, {"_id": 0}, max_time_ms=3000) or {}
    return {**DEFAULTS, **doc}


def in_scope(risk_reason: Optional[str]) -> bool:
    if not risk_reason:
        return False
    if risk_reason.startswith("entry_timing:"):
        return True
    if risk_reason.startswith("risk_sizer:"):
        return risk_reason.split(":", 1)[1] in _SIZER_SCOPE
    return False


def block_price(intent: dict) -> Optional[float]:
    """Frozen price at the moment of the block — same derivation
    ladder as the entry timing gate."""
    et = intent.get("entry_timing_receipt") or {}
    p = _f(et.get("confirmation_price"))
    if p:
        return p
    snap = intent.get("snapshot") or {}
    bid, ask = _f(snap.get("bid")), _f(snap.get("ask"))
    if bid and ask and ask >= bid:
        return (bid + ask) / 2.0
    return _f(snap.get("price")) or _f(intent.get("price_at_signal"))


def classify_outcome(entry: float, bars: list[dict],
                     tp_pct: float, sl_pct: float) -> dict:
    """Pure counterfactual verdict over chronological bars AFTER the
    block. First-touch of TP/SL decides; both in one bar → sl_hit
    conservative + ambiguous flag. Peak/trough span the FULL horizon
    (informational, regardless of the early exit)."""
    tp = entry * (1.0 + tp_pct / 100.0)
    sl = entry * (1.0 - sl_pct / 100.0)
    peak, trough = entry, entry
    outcome, ambiguous, decided = "expired", False, False
    for b in bars:
        c = _f(b.get("c")) or 0.0
        h = _f(b.get("h")) or c
        l = _f(b.get("l")) or c
        peak = max(peak, h)
        if l > 0:
            trough = min(trough, l)
        if not decided:
            hit_tp, hit_sl = h >= tp, 0 < l <= sl
            if hit_tp and hit_sl:
                outcome, ambiguous, decided = "sl_hit", True, True
            elif hit_sl:
                outcome, decided = "sl_hit", True
            elif hit_tp:
                outcome, decided = "tp_hit", True
    end = _f(bars[-1].get("c")) or entry if bars else entry
    return {
        "outcome": outcome, "ambiguous": ambiguous,
        "peak_pct": round((peak / entry - 1.0) * 100.0, 3),
        "trough_pct": round((trough / entry - 1.0) * 100.0, 3),
        "end_pct": round((end / entry - 1.0) * 100.0, 3),
    }


async def _bars_after(db, symbol: str, lane: str,
                      start_iso: str, end_iso: str) -> list[dict]:
    q = {"symbol": symbol, "tf": "5m",
         "ts": {"$gte": start_iso, "$lte": end_iso}}
    proj = {"_id": 0, "ts": 1, "h": 1, "l": 1, "c": 1}
    rows = await db["shared_ohlcv_bars"].find(q, proj).sort(
        "ts", 1).max_time_ms(5000).to_list(300)
    if lane == "crypto" and len(rows) < 12:
        try:
            from shared.feeders.kraken_ohlc import (  # noqa: WPS433
                _fetch_and_persist_one,
            )
            start_dt = datetime.fromisoformat(start_iso)
            days = ((_now() - start_dt).total_seconds() / 86400.0) + 0.05
            await _fetch_and_persist_one(symbol, min(days, 2.5), tf="5m")
            rows = await db["shared_ohlcv_bars"].find(q, proj).sort(
                "ts", 1).max_time_ms(5000).to_list(300)
        except Exception as exc:  # noqa: BLE001
            logger.warning("missed_entries backfill failed %s: %s",
                           symbol, exc)
    return rows


async def _exit_pcts(intent: dict) -> tuple[float, float]:
    """(tp_pct, sl_pct) the counterfactual position would have used."""
    if (intent.get("stack") or "") == "momentum":
        try:
            from momentum.momentum_scanner import get_momentum_exit_pcts  # noqa: WPS433
            return await get_momentum_exit_pcts()
        except Exception:  # noqa: BLE001
            pass
    try:
        from shared.exits.policy import get_policy  # noqa: WPS433
        lp = (await get_policy()).get(intent.get("lane") or "crypto") or {}
        return (float(lp.get("tp_pct") or 5.0),
                float(lp.get("sl_pct") or 3.0))
    except Exception:  # noqa: BLE001
        return 5.0, 3.0


async def run_cycle() -> dict:
    from db import db  # noqa: WPS433
    from namespaces import SHARED_INTENTS  # noqa: WPS433
    cfg = await get_config()
    if not cfg.get("enabled", True):
        return {"skipped": "disabled"}
    horizon_h = float(cfg["horizon_h"])
    max_eval = int(cfg["max_eval_per_cycle"])
    now = _now()
    newest = (now - timedelta(hours=horizon_h)).isoformat()
    oldest = (now - timedelta(hours=60)).isoformat()  # 5m backfill reach
    stats = {"scanned": 0, "evaluated": 0, "no_data": 0}

    rows = await db[SHARED_INTENTS].find(
        {"action": "BUY", "gate_state": "blocked",
         "risk_reason": {"$regex": "^(entry_timing|risk_sizer):"},
         "ingest_ts": {"$gte": oldest, "$lte": newest}},
        {"_id": 0, "intent_id": 1, "symbol": 1, "lane": 1, "stack": 1,
         "risk_reason": 1, "ingest_ts": 1, "last_submit_ts": 1,
         "price_at_signal": 1, "snapshot": 1, "entry_timing_receipt": 1},
    ).sort("ingest_ts", -1).max_time_ms(8000).to_list(200)

    for intent in rows:
        if stats["evaluated"] + stats["no_data"] >= max_eval:
            break
        reason = intent.get("risk_reason") or ""
        intent_id = intent.get("intent_id") or ""
        if not intent_id or not in_scope(reason):
            continue
        stats["scanned"] += 1
        doc_id = f"missed:{intent_id}"
        if await db[COLLECTION].find_one({"_id": doc_id}, {"_id": 1},
                                         max_time_ms=3000):
            continue
        blocked_at = intent.get("last_submit_ts") or intent["ingest_ts"]
        entry = block_price(intent)
        base = {
            "_id": doc_id, "intent_id": intent_id,
            "symbol": intent.get("symbol"), "lane": intent.get("lane"),
            "stack": intent.get("stack"), "block_reason": reason,
            "blocked_at": blocked_at, "horizon_h": horizon_h,
            "evaluated_at": _now().isoformat(),
        }
        if not entry:
            await db[COLLECTION].update_one(
                {"_id": doc_id},
                {"$set": {**base, "outcome": "no_data",
                          "detail": "no_block_price"}}, upsert=True)
            stats["no_data"] += 1
            continue
        end_iso = (datetime.fromisoformat(blocked_at)
                   + timedelta(hours=horizon_h)).isoformat()
        bars = await _bars_after(db, intent.get("symbol") or "",
                                 intent.get("lane") or "crypto",
                                 blocked_at, end_iso)
        if len(bars) < 6:
            await db[COLLECTION].update_one(
                {"_id": doc_id},
                {"$set": {**base, "outcome": "no_data",
                          "detail": f"bars={len(bars)}"}}, upsert=True)
            stats["no_data"] += 1
            continue
        tp_pct, sl_pct = await _exit_pcts(intent)
        verdict = classify_outcome(entry, bars, tp_pct, sl_pct)
        await db[COLLECTION].update_one(
            {"_id": doc_id},
            {"$set": {**base, "entry_price": entry, "tp_pct": tp_pct,
                      "sl_pct": sl_pct, "bars_used": len(bars),
                      **verdict}}, upsert=True)
        stats["evaluated"] += 1
        logger.info("missed_entries: %s %s → %s (peak %+.2f%% end %+.2f%%)",
                    intent.get("symbol"), reason, verdict["outcome"],
                    verdict["peak_pct"], verdict["end_pct"])
    return stats


async def ledger_stats(db, hours: int = 168) -> dict:
    cut = (_now() - timedelta(hours=hours)).isoformat()
    by_reason: dict[str, dict] = {}
    async for r in db[COLLECTION].aggregate([
        {"$match": {"evaluated_at": {"$gte": cut},
                    "outcome": {"$ne": "no_data"}}},
        {"$group": {
            "_id": "$block_reason", "n": {"$sum": 1},
            "avg_peak_pct": {"$avg": "$peak_pct"},
            "avg_end_pct": {"$avg": "$end_pct"},
            "tp": {"$sum": {"$cond": [{"$eq": ["$outcome", "tp_hit"]}, 1, 0]}},
            "sl": {"$sum": {"$cond": [{"$eq": ["$outcome", "sl_hit"]}, 1, 0]}},
        }},
        {"$sort": {"n": -1}},
    ], maxTimeMS=8000):
        by_reason[r["_id"]] = {
            "n": r["n"], "would_tp": r["tp"], "would_sl": r["sl"],
            "avg_peak_pct": round(r["avg_peak_pct"] or 0, 2),
            "avg_end_pct": round(r["avg_end_pct"] or 0, 2),
        }
    no_data = await db[COLLECTION].count_documents(
        {"evaluated_at": {"$gte": cut}, "outcome": "no_data"},
        maxTimeMS=5000)
    recent = await db[COLLECTION].find(
        {"evaluated_at": {"$gte": cut}}, {"_id": 0},
    ).sort("blocked_at", -1).max_time_ms(5000).to_list(25)
    return {"by_reason": by_reason, "no_data": no_data, "recent": recent}


async def worker_loop() -> None:
    logger.info("missed-entry ledger started")
    while True:
        try:
            await asyncio.sleep(600)
            await run_cycle()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("missed_entries loop error: %s", exc)
