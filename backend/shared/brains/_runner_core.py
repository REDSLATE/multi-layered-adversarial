"""Shared runner core for all native brain runtimes.

The operator's architectural pin:
    brains think separately   ← `shared/brains/<brain>/strategy.py`
    MC schedules them together ← THIS MODULE + per-brain runtime shims

Doctrine code (the interpretation function) stays per-brain. The
mechanics of "iterate the equity universe, load each snapshot, run
the strategy, emit via canonical path" is identical across brains and
lives here. This avoids 4 nearly-identical 160-line runner files
drifting out of sync.

Each per-brain `shared/brains/<brain>/runner.py` is a thin shim that
binds its strategy + brain identity to `run_tick_for_brain` below.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Literal, Optional

from shared.intents import IntentIn, submit_intent_in_process


logger = logging.getLogger("risedual.brains.runner_core")

UNIVERSE_COLLECTION = "patterns_universe"
SNAPSHOTS_COLLECTION = "shared_indicator_snapshots"


@dataclass(frozen=True)
class StrategyDecision:
    """Wire-protocol for per-brain strategy outputs. Mirrors the
    `Decision` dataclass each `shared/brains/<brain>/strategy.py`
    returns — duck-typed so we don't need to import every brain's
    dataclass into this core module.
    """
    action: Literal["BUY", "SHORT", "HOLD"]
    confidence: float
    size_bias: float
    rationale: str
    target_price: Optional[float]
    stop_price: Optional[float]
    evidence: dict[str, Any]
    skipped_reason: Optional[str]


# Strategy callable signature: (symbol, indicators) → decision-like obj.
StrategyFn = Callable[[str, dict[str, Any]], Any]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _load_equity_universe(db) -> list[str]:
    """Pull the equity universe. Symbols without an explicit `lane`
    are treated as equity (legacy seed docs)."""
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
    return await db[SNAPSHOTS_COLLECTION].find_one(
        {"symbol": symbol},
        {"_id": 0},
        sort=[("computed_at", -1)],
    )


def _build_intent_body(
    *,
    brain_id: str,
    symbol: str,
    decision: Any,
    runtime_version: str,
) -> IntentIn:
    return IntentIn(
        stack=brain_id,            # type: ignore[arg-type]
        action=decision.action,
        symbol=symbol,
        lane="equity",
        confidence=float(decision.confidence),
        risk_multiplier=0.0,
        rationale=decision.rationale,
        target_price=decision.target_price,
        stop_price=decision.stop_price,
        evidence={
            **(decision.evidence or {}),
            "emit_source": f"{brain_id}_native_runtime",
            "emit_source_version": runtime_version,
        },
        raw_action=decision.action,
        raw_confidence=float(decision.confidence),
        market_decision=decision.action,
        execution_decision="ALLOW",
        display_action=decision.action,
        intent_version="v2",
    )


def _aggregate_reasons(skipped: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in skipped:
        reason = str(row.get("reason") or "unknown")
        key = reason.split(":", 1)[0]
        counts[key] = counts.get(key, 0) + 1
    return counts


async def run_tick_for_brain(
    *,
    db,
    brain_id: str,
    strategy_fn: StrategyFn,
    tick_log_collection: str,
    runtime_version: str = "v1",
    pre_emit_hook: Optional[Callable[[str, Any], Awaitable[None]]] = None,
) -> dict[str, Any]:
    """One pass over the equity universe for `brain_id`.

    Args:
        db: motor db handle
        brain_id: canonical brain identifier (`barracuda`, `gto`, …)
        strategy_fn: per-brain `evaluate(symbol, indicators) -> Decision`
        tick_log_collection: `<brain>_native_runtime_ticks` — persists
            one summary doc per tick so a missing tick is visible
        runtime_version: stamps `emit_source_version` on every intent
        pre_emit_hook: optional async callback (symbol, decision) → None
            invoked just before `submit_intent_in_process` — used by
            tests to instrument the loop without monkey-patching.

    Returns the summary dict; same shape is persisted to the
    per-brain tick log collection.
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
            decision = strategy_fn(symbol, indicators)
            if decision.action == "HOLD":
                skipped.append({
                    "symbol": symbol,
                    "reason": getattr(decision, "skipped_reason", None),
                })
                continue
            if pre_emit_hook is not None:
                await pre_emit_hook(symbol, decision)
            body = _build_intent_body(
                brain_id=brain_id, symbol=symbol,
                decision=decision, runtime_version=runtime_version,
            )
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
                "%s runner symbol=%s failed: %r", brain_id, symbol, exc,
            )
            errors.append({
                "symbol": symbol,
                "error_type": type(exc).__name__,
                "error_message": str(exc)[:300],
            })

    finished_at = _now_iso()
    summary = {
        "brain_id": brain_id,
        "started_at": started_at,
        "finished_at": finished_at,
        "universe_size": len(universe),
        "emitted_count": len(emitted),
        "skipped_count": len(skipped),
        "no_snapshot_count": len(no_snapshot),
        "error_count": len(errors),
        "emitted": emitted,
        "errors": errors,
        "skipped_reasons": _aggregate_reasons(skipped),
        "no_snapshot_symbols": no_snapshot[:50],
        "runtime": f"{brain_id}_native_{runtime_version}",
    }

    try:
        await db[tick_log_collection].insert_one(dict(summary))
    except Exception as exc:  # noqa: BLE001
        logger.warning("%s tick row persist failed: %r", brain_id, exc)

    return summary


__all__ = ["run_tick_for_brain", "StrategyDecision"]
