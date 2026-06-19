"""Paradox MA-canary runner — fire one canary signal through the pipeline.

Bridges `paradox_momentum_vote.MomentumSignal` to a real
`shared_intents` row tagged with `source=ma_canary`. Once the row
lands, the auto-router picks it up on the next tick, runs it through
SeatPolicy → Governor → RoadGuard → Broker, and writes a
`pipeline_receipts` row that the operator can trace.

Kill switch:
    PARADOX_MA_CANARY_ENABLED=false   (default OFF — fail-CLOSED)
    PARADOX_MA_CANARY_NOTIONAL=10
    PARADOX_MA_CANARY_LANE=equity     (informational — actual lane
                                       comes from the fire call)

Brain attribution is ROSTER-AWARE: the canary writes the intent under
the current executor seat holder (e.g. barracuda for equity right
now). If the operator rotates the seat tomorrow, the next canary
fire will automatically attribute to the new holder. This avoids
the GTO-was-auditor trap.

Read-only on broker. The runner only INSERTS into shared_intents and
returns the intent_id — the auto-router does the actual broker call.
"""
from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Optional

from db import db
from namespaces import BRAIN_ROSTER, SHARED_INTENTS, SHARED_OHLCV_BARS

from .paradox_momentum_vote import (
    Lane,
    MomentumSignal,
    moving_average_momentum,
)


logger = logging.getLogger("paradox.ma_canary")


_DEFAULT_NOTIONAL = float(os.environ.get("PARADOX_MA_CANARY_NOTIONAL", "10"))


# Lane → roster key for the executor seat holder. Same bridge that
# `shared/pipeline/seat_policy.py` uses, intentionally duplicated
# here to keep this module standalone (no upward-looking imports).
_LANE_TO_ROSTER_KEY = {"equity": "executor", "crypto": "crypto"}


def is_canary_enabled() -> bool:
    """Kill switch — env var only. Mongo flag could be added later if
    the operator wants a phone-flippable toggle, but for the initial
    plumbing-validation pass an env var keeps the surface tiny."""
    return os.environ.get("PARADOX_MA_CANARY_ENABLED", "false").lower() == "true"


async def _current_executor(lane: Lane) -> Optional[str]:
    """Look up the brain currently holding the executor seat for this
    lane — the only brain that should attribute a canary intent."""
    roster_key = _LANE_TO_ROSTER_KEY.get(lane)
    if not roster_key:
        return None
    doc = await db[BRAIN_ROSTER].find_one(
        {"_id": "current"}, {"_id": 0, "assignments": 1},
    )
    if not doc:
        return None
    return ((doc.get("assignments") or {}).get(roster_key)) or None


async def _fetch_recent_closes(
    symbol: str,
    bars: int = 60,
    timeframe: str = "1h",
) -> list[float]:
    """Read the last `bars` close prices for `symbol` at `timeframe`
    from `shared_ohlcv_bars`. Newest first → oldest last for the
    strategy (chronological order).
    """
    rows = await db[SHARED_OHLCV_BARS].find(
        {"symbol": symbol, "tf": timeframe},
        {"_id": 0, "c": 1, "ts": 1},
    ).sort("ts", -1).limit(bars).to_list(bars)
    rows.reverse()
    return [float(r.get("c") or 0) for r in rows]


async def fire_canary(
    symbol: str,
    lane: Lane = "equity",
    timeframe: str = "1h",
    notional_usd: Optional[float] = None,
    fast_window: int = 10,
    slow_window: int = 30,
) -> dict:
    """Fire one canary intent end-to-end.

    Returns the freshly-inserted intent dict (no `_id`) plus the
    signal that produced it, so the operator can trace the chain:

        signal.reason         → why HOLD / BUY / SELL
        signal.confidence     → derived from MA gap
        intent_id             → match against pipeline_receipts.intent_id

    Caller decides if/when to call this. The function does NOT
    self-loop; it's one-shot.
    """
    if not is_canary_enabled():
        return {
            "ok": False,
            "reason": "canary_kill_switch_disabled",
            "hint": "Set PARADOX_MA_CANARY_ENABLED=true in backend/.env to enable.",
        }

    holder = await _current_executor(lane)
    if not holder:
        return {
            "ok": False,
            "reason": "no_executor_seat_holder",
            "hint": f"Lane {lane!r} has no current executor in the roster. Assign one via QSS.",
        }

    closes = await _fetch_recent_closes(symbol, bars=slow_window * 2, timeframe=timeframe)
    if len(closes) < slow_window:
        return {
            "ok": False,
            "reason": "insufficient_market_data",
            "hint": (
                f"Only {len(closes)} bars of {timeframe} data for {symbol!r}; "
                f"need {slow_window}. Check feeders are running."
            ),
        }

    signal: MomentumSignal = moving_average_momentum(
        symbol=symbol,
        closes=closes,
        lane=lane,
        fast_window=fast_window,
        slow_window=slow_window,
    )

    # HOLD signals don't produce broker intents. Return the signal so
    # the operator can see why nothing fired — useful diagnostic when
    # the market is flat or the warmup window is too short.
    if signal.action == "HOLD":
        return {
            "ok": True,
            "action": "HOLD",
            "intent_id": None,
            "signal": _signal_to_json(signal),
            "note": "Canary computed HOLD — no intent written.",
        }

    intent_id = uuid.uuid4().hex
    notional = float(notional_usd if notional_usd is not None else _DEFAULT_NOTIONAL)
    now = datetime.now(timezone.utc).isoformat()

    # Intent shape mirrors what `shared_intents.insert_one(slim_doc)`
    # at line 478 of shared/intents.py writes — same fields the
    # auto-router reads, same fields the existing UI renders. Tagged
    # so any post-hoc analysis can filter by `source=ma_canary`.
    intent_doc = {
        "intent_id": intent_id,
        "stack": holder,
        "symbol": symbol,
        "action": signal.action,
        "lane": lane,
        "confidence": signal.confidence,
        "gate_state": "pending",
        "executed": False,
        "holds_executor_seat": True,  # we just looked up the holder
        "executor_holder_at_post": holder,
        "requested_notional_usd": notional,
        "ingest_ts": now,
        "created_at": now,
        "source": "ma_canary",
        "evidence": {
            **signal.evidence,
            "source": "ma_canary",
            "kill_switch": "PARADOX_MA_CANARY_ENABLED",
            "reason": signal.reason,
            "fast_ma": signal.fast_ma,
            "slow_ma": signal.slow_ma,
            "ma_gap_pct": signal.ma_gap_pct,
            "paradox_rule": "strategy_testifies_never_executes",
            "timeframe": timeframe,
        },
    }

    await db[SHARED_INTENTS].insert_one(intent_doc)
    logger.info(
        "ma_canary FIRED: symbol=%s lane=%s action=%s conf=%.3f notional=$%.2f intent=%s holder=%s",
        symbol, lane, signal.action, signal.confidence, notional, intent_id, holder,
    )

    # Strip the `_id` ObjectId before returning so the response is
    # JSON-safe (insert_one mutates the dict by adding `_id`).
    intent_doc.pop("_id", None)
    return {
        "ok": True,
        "action": signal.action,
        "intent_id": intent_id,
        "holder": holder,
        "signal": _signal_to_json(signal),
        "intent": intent_doc,
        "next_step": (
            "Auto-router picks this up on the next tick. Watch "
            f"GET /api/intents/{intent_id}/why for the receipt."
        ),
    }


def _signal_to_json(signal: MomentumSignal) -> dict:
    return {
        "action": signal.action,
        "confidence": signal.confidence,
        "fast_ma": signal.fast_ma,
        "slow_ma": signal.slow_ma,
        "ma_gap_pct": signal.ma_gap_pct,
        "reason": signal.reason,
        "evidence": signal.evidence,
    }
