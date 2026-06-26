"""Barracuda runner — one-tick decision cycle, in-process.

Each tick:
  1. Pull Barracuda's equity universe from `patterns_universe`
     (lane='equity' or lane absent — Barracuda is muted on crypto by
     `brain_lane_policy.is_brain_lane_allowed`, so we don't even ask).
  2. For each symbol, load the freshest `shared_indicator_snapshots`
     row.
  3. Run `strategy.evaluate(symbol, indicators)`.
  4. If the decision is BUY/SHORT, build an `IntentIn` and call
     `submit_intent_in_process` — NO HTTP, NO runtime-token roundtrip,
     NO sidecar.

Side effects per tick:
  * Writes a heartbeat row to `barracuda_native_runtime_ticks` so the
    operator can confirm the loop is alive even when no signal fires.
  * Writes one `shared_intents` doc per emitted decision (via the
    canonical `_post_intent_impl` path, identical to what an external
    sidecar would have written).

Doctrine guarantees:
  * No silent failures. Symbol-level exceptions are caught and stamped
    on the tick row; they never abort the tick.
  * No identity drift. `stack="barracuda"` is the only identity emitted.
  * No HTTP self-loop. We bypass FastAPI entirely.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from shared.brains.barracuda import strategy as barracuda_strategy
from shared.intents import IntentIn, submit_intent_in_process


logger = logging.getLogger("risedual.brains.barracuda.runner")

UNIVERSE_COLLECTION = "patterns_universe"
SNAPSHOTS_COLLECTION = "shared_indicator_snapshots"
TICK_LOG_COLLECTION = "barracuda_native_runtime_ticks"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _load_equity_universe(db) -> list[str]:
    """Pull the equity universe Barracuda is allowed to trade.

    Returns sorted unique tickers. A doc without an explicit `lane`
    field is treated as equity (legacy seed docs were lane-less).
    """
    cursor = db[UNIVERSE_COLLECTION].find(
        {"$or": [{"lane": "equity"}, {"lane": {"$exists": False}}]},
        {"_id": 0, "symbol": 1},
    )
    symbols: set[str] = set()
    async for row in cursor:
        s = (row.get("symbol") or "").strip().upper()
        if s and s.isalnum():
            symbols.add(s)
    return sorted(symbols)


async def _latest_indicator_snapshot(db, symbol: str) -> Optional[dict]:
    """Return the freshest snapshot for `symbol` regardless of TF.
    Prefer the multi-TF `1h` slot when present; otherwise the latest
    `computed_at`.
    """
    row = await db[SNAPSHOTS_COLLECTION].find_one(
        {"symbol": symbol},
        {"_id": 0},
        sort=[("computed_at", -1)],
    )
    return row


def _build_intent_body(
    symbol: str,
    decision: barracuda_strategy.Decision,
    indicators: dict[str, Any],
) -> IntentIn:
    """Translate a `Decision` into the canonical `IntentIn` envelope."""
    return IntentIn(
        stack="barracuda",
        action=decision.action,  # "BUY" or "SHORT"
        symbol=symbol,
        lane="equity",
        confidence=float(decision.confidence),
        risk_multiplier=0.0,
        rationale=decision.rationale,
        target_price=decision.target_price,
        stop_price=decision.stop_price,
        evidence={
            **(decision.evidence or {}),
            "emit_source": "barracuda_native_runtime",
            "emit_source_version": "v1",
        },
        regime=None,
        raw_action=decision.action,
        raw_confidence=float(decision.confidence),
        market_decision=decision.action,
        execution_decision="ALLOW",
        display_action=decision.action,
        intent_version="v2",
    )


async def tick_once(db) -> dict[str, Any]:
    """One full pass over the universe. Returns a summary dict so the
    scheduler loop can log it and operators can inspect tick history.

    The summary is also persisted to `barracuda_native_runtime_ticks`
    so a missing tick is visible (silent worker → visible worker).
    """
    started_at = _now_iso()
    universe = await _load_equity_universe(db)

    emitted: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    no_snapshot: list[str] = []

    for symbol in universe:
        try:
            snap = await _latest_indicator_snapshot(db, symbol)
            if not snap:
                no_snapshot.append(symbol)
                continue
            indicators = snap.get("indicators") or {}
            decision = barracuda_strategy.evaluate(symbol, indicators)
            if decision.action == "HOLD":
                skipped.append({
                    "symbol": symbol,
                    "reason": decision.skipped_reason,
                })
                continue
            body = _build_intent_body(symbol, decision, indicators)
            result = await submit_intent_in_process(body)
            emitted.append({
                "symbol": symbol,
                "action": decision.action,
                "confidence": decision.confidence,
                "intent_id": result.get("intent_id"),
                "gate_state": result.get("gate_state"),
            })
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "barracuda runner symbol=%s failed: %r", symbol, exc,
            )
            errors.append({
                "symbol": symbol,
                "error_type": type(exc).__name__,
                "error_message": str(exc)[:300],
            })

    finished_at = _now_iso()
    summary = {
        "started_at": started_at,
        "finished_at": finished_at,
        "universe_size": len(universe),
        "emitted_count": len(emitted),
        "skipped_count": len(skipped),
        "no_snapshot_count": len(no_snapshot),
        "error_count": len(errors),
        "emitted": emitted,
        "errors": errors,
        # Skips & no_snapshot are kept compact so a 200-symbol universe
        # doesn't bloat the tick row.
        "skipped_reasons": _aggregate_reasons(skipped),
        "no_snapshot_symbols": no_snapshot[:50],
        "runtime": "barracuda_native_v1",
    }

    try:
        await db[TICK_LOG_COLLECTION].insert_one(dict(summary))
    except Exception as exc:  # noqa: BLE001
        logger.warning("barracuda tick row persist failed: %r", exc)

    return summary


def _aggregate_reasons(skipped: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in skipped:
        reason = str(row.get("reason") or "unknown")
        # Keep the prefix before any ':' so similar reasons collapse.
        key = reason.split(":", 1)[0]
        counts[key] = counts.get(key, 0) + 1
    return counts


__all__ = ["tick_once"]
