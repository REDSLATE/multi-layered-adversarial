"""Entry Re-arm Watcher — recover the RIGHT moment (2026-08-01).

Operator doctrine: "The Entry Timing Gate should block the wrong
moment. The Trigger Watcher must recover the right moment." Narrowly
scoped: ONLY BUY intents blocked by the timing gate for extension/
phase reasons create a WAIT_FOR_PULLBACK trigger. Risk/allowlist/
data/broker rejections never re-arm.

Flow: timing block → trigger (WATCHING) → watch live bars → pullback
forms (highs stop expanding, depth in policy, volume contracts, no
lower-low breakdown, support holds) → momentum RESUMES (reacceleration
bar) → emit a NEW CHILD intent (fresh intent_id, lineage fields, new
confirmation price = reacceleration close) → child runs the FULL gate
chain again (seat, risk, timing vs the NEW confirmation, broker).
The original intent is never pushed back through unchanged, and the
old confirmation is never reused: price falling is not an entry,
price stabilizing is not an entry, price reaccelerating after support
holds IS an entry candidate.

Trigger terminal states (feed the Timing tile):
  REARMED     — pullback + reacceleration found, child intent emitted
  EXPIRED     — window closed; price continued without a pullback
  INVALIDATED — structure broke down below invalidation
Config: runtime_flags.entry_timing → {"rearm": {...}} (shares the
gate's flag doc). Watcher ships ENABLED.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

logger = logging.getLogger("risedual.entry_rearm")

TRIGGERS = "entry_rearm_triggers"

REARMABLE_REASONS = frozenset({
    "MISSED_ENTRY_CHASE_RISK", "MOVE_ALREADY_EXTENDED",
    "PARABOLIC_CHASE_RISK", "LATE_MOMENTUM_ENTRY", "TOO_FAR_ABOVE_VWAP",
})

DEFAULT_REARM = {
    "enabled": True,
    "watch_window_minutes": 240,
    "min_pullback_pct": 2.0,
    "max_pullback_pct": 30.0,
    "volume_contraction_ratio": 0.8,
    "max_attempts": 1,
    "tick_seconds": 30,
}

_TASK: Optional[asyncio.Task] = None


def _ema(closes: list[float], period: int = 9) -> Optional[float]:
    if len(closes) < period:
        return None
    k = 2.0 / (period + 1)
    e = sum(closes[:period]) / period
    for c in closes[period:]:
        e = c * k + e * (1 - k)
    return e


def _session_vwap(bars: list[dict]) -> Optional[float]:
    day = str(bars[-1].get("ts") or "")[:10]
    sess = [b for b in bars if str(b.get("ts") or "")[:10] == day]
    use = sess if len(sess) >= 3 else bars
    num = den = 0.0
    for b in use:
        tp = (float(b.get("h") or 0) + float(b.get("l") or 0)
              + float(b.get("c") or 0)) / 3.0
        v = float(b.get("v") or 0)
        num += tp * v
        den += v
    return num / den if den > 0 else None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


async def get_rearm_config() -> dict:
    from shared.risk_sizer.entry_timing import get_config  # noqa: WPS433
    from db import db  # noqa: WPS433
    doc = await db["runtime_flags"].find_one(
        {"_id": "entry_timing"}, {"_id": 0, "rearm": 1}, max_time_ms=3000,
    ) or {}
    cfg = {**DEFAULT_REARM, **(doc.get("rearm") or {})}
    cfg["gate_enabled"] = (await get_config())["enabled"]
    return cfg


async def create_trigger(intent: dict, reason: str, receipt: dict) -> None:
    """Called from the timing gate's block path. One WATCHING trigger
    per (symbol, lane) — repeated blocks refresh the peak, not stack."""
    if (intent.get("action") or "").upper() != "BUY":
        return
    if reason not in REARMABLE_REASONS:
        return
    cfg = await get_rearm_config()
    if not cfg["enabled"]:
        return
    from db import db  # noqa: WPS433
    from shared.retention import ttl_stamp  # noqa: WPS433
    symbol = (intent.get("symbol") or "").upper()
    lane = (intent.get("lane") or "equity").lower()
    block_price = receipt.get("current_price")
    existing = await db[TRIGGERS].find_one(
        {"symbol": symbol, "lane": lane, "state": "WATCHING"},
        {"_id": 1}, max_time_ms=3000,
    )
    if existing:
        return
    await db[TRIGGERS].insert_one({
        "trigger_id": str(uuid.uuid4()),
        "state": "WATCHING",
        "symbol": symbol,
        "lane": lane,
        "stack": intent.get("stack"),
        "original_intent_id": intent.get("intent_id"),
        "timing_block_reason": reason,
        "timing_block_receipt": receipt,
        "original_confirmation_price": receipt.get("confirmation_price"),
        "block_price": block_price,
        "peak_price": block_price,
        "last_price": block_price,
        "attempts": 0,
        "created_at": _now_iso(),
        "expires_at": (_now() + timedelta(
            minutes=cfg["watch_window_minutes"])).isoformat(),
        "ttl_at": ttl_stamp(7),
    })
    logger.info("entry_rearm: WATCHING %s after %s block @ %s",
                symbol, reason, block_price)


# ─── pure pullback / reacceleration detector ────────────────────────

def detect_pullback_reentry(bars: list[dict], peak_price: float,
                            invalidation_price: float,
                            cfg: dict) -> tuple[str, dict]:
    """('reenter'|'wait'|'invalidated', receipt). bars oldest→newest.
    Falling ≠ entry. Stabilizing ≠ entry. Reaccelerating after
    support holds = entry candidate."""
    if len(bars) < 8:
        return "wait", {"why": "thin_bars"}
    closes = [float(b.get("c") or 0) for b in bars]
    opens = [float(b.get("o") or 0) for b in bars]
    highs = [float(b.get("h") or 0) for b in bars]
    lows = [float(b.get("l") or 0) for b in bars]
    vols = [float(b.get("v") or 0) for b in bars]
    price = closes[-1]
    receipt: dict[str, Any] = {"price": price, "peak_price": peak_price}

    if invalidation_price and lows[-1] < invalidation_price:
        receipt["why"] = "structure_breakdown"
        return "invalidated", receipt

    pull_low = min(lows[-6:])
    depth = (peak_price - pull_low) / peak_price * 100.0 if peak_price else 0.0
    receipt["pullback_depth_pct"] = round(depth, 2)
    if depth < cfg["min_pullback_pct"]:
        receipt["why"] = "no_pullback_yet"
        return "wait", receipt
    if depth > cfg["max_pullback_pct"]:
        receipt["why"] = "pullback_too_deep"
        return "invalidated", receipt

    # highs stopped expanding
    if max(highs[-3:]) >= peak_price:
        receipt["why"] = "still_making_highs"
        return "wait", receipt
    # no lower-low breakdown at the end (stabilization)
    if lows[-1] < min(lows[-4:-1]):
        receipt["why"] = "still_falling"
        return "wait", receipt
    # volume contracts during the pullback vs the run-up window
    run_v = sum(vols[-12:-6]) / 6.0
    pull_v = sum(vols[-6:-1]) / 5.0
    receipt["volume_contraction"] = round(pull_v / run_v, 2) if run_v else None
    if run_v and pull_v > run_v * cfg["volume_contraction_ratio"]:
        receipt["why"] = "no_volume_contraction"
        return "wait", receipt
    # support holds — "breakout level, VWAP, or short EMA holds":
    # ANY structural floor under the pullback low qualifies (VWAP is
    # naturally overhead after a deep pullback; EMA9 tracks the base)
    ema9 = _ema(closes, 9)
    vwap = _session_vwap(bars)
    supports = [s for s in (ema9, vwap) if s]
    receipt["support"] = {"ema9": round(ema9, 6) if ema9 else None,
                          "vwap": round(vwap, 6) if vwap else None}
    if supports and not any(
        lows[-1] >= s * 0.99 or closes[-1] >= s for s in supports
    ):
        receipt["why"] = "support_lost"
        return "wait", receipt
    # reacceleration: green bar taking out prior high with volume pickup
    reaccel = (
        closes[-1] > opens[-1]
        and closes[-1] > highs[-2]
        and closes[-1] > closes[-2]
        and (not pull_v or vols[-1] >= pull_v * 1.1)
    )
    if not reaccel:
        receipt["why"] = "stabilizing_not_reaccelerating"
        return "wait", receipt
    receipt["why"] = "pullback_held_and_reaccelerated"
    receipt["new_confirmation_price"] = price
    receipt["new_invalidation_price"] = round(pull_low, 6)
    return "reenter", receipt


# ─── child intent emission ──────────────────────────────────────────

async def _emit_child_intent(trigger: dict, receipt: dict) -> Optional[str]:
    from db import db  # noqa: WPS433
    from namespaces import SHARED_INTENTS  # noqa: WPS433
    orig = await db[SHARED_INTENTS].find_one(
        {"intent_id": trigger["original_intent_id"]}, {"_id": 0},
        max_time_ms=4000,
    )
    if not orig:
        return None
    rearm_attempt_id = str(uuid.uuid4())
    child = dict(orig)
    for stale in (
        "gate_state", "executed", "risk_reason", "broker_reason",
        "broker_error_bucket", "broker_order", "last_submit_ts",
        "entry_timing_decision", "entry_timing_reason",
        "entry_timing_receipt", "expired_reason", "expired_at", "ttl_at",
        "route_timeouts", "last_route_timeout_at", "executed_at",
        "execution", "execution_receipt_id",
    ):
        child.pop(stale, None)
    child.update({
        "intent_id": rearm_attempt_id,
        "ingest_ts": _now_iso(),
        "gate_state": "pending",
        "executed": False,
        "snapshot": {**(orig.get("snapshot") or {}),
                     "price": receipt["new_confirmation_price"]},
        "rearm_of": trigger["original_intent_id"],
        "trigger_id": trigger["trigger_id"],
        "rearm_attempt_id": rearm_attempt_id,
        "timing_block_receipt_id": trigger["original_intent_id"],
        "new_confirmation_price": receipt["new_confirmation_price"],
        "new_invalidation_price": receipt["new_invalidation_price"],
        "stop_price": receipt["new_invalidation_price"],
    })
    await db[SHARED_INTENTS].insert_one(child)
    # Mirror into the LOCAL durable intent queue — the auto-router
    # picks from it per-tick (Atlas is only an error fallback), so a
    # Mongo-only insert would never be routed (2026-08-01 finding).
    try:
        from shared.hotpath import intent_queue  # noqa: WPS433
        intent_queue.enqueue_safe(child)
    except Exception as exc:  # noqa: BLE001
        logger.warning("entry_rearm: child enqueue failed %s: %s",
                       rearm_attempt_id[:8], exc)
    return rearm_attempt_id


# ─── watcher loop ───────────────────────────────────────────────────

async def _tick() -> int:
    from db import worker_db as db  # noqa: WPS433
    from shared.risk_sizer.entry_timing import _load_bars  # noqa: WPS433
    cfg = await get_rearm_config()
    if not cfg["enabled"]:
        return 0
    acted = 0
    rows = await db[TRIGGERS].find(
        {"state": "WATCHING"}, {"_id": 0},
    ).max_time_ms(8000).to_list(50)
    for trig in rows:
        try:
            if str(trig.get("expires_at") or "") < _now_iso():
                await db[TRIGGERS].update_one(
                    {"trigger_id": trig["trigger_id"]},
                    {"$set": {"state": "EXPIRED",
                              "state_reason": "window_closed_no_pullback",
                              "state_ts": _now_iso()}})
                continue
            bars = await _load_bars(trig["symbol"])
            if not bars:
                continue
            price = float(bars[-1].get("c") or 0)
            high = float(bars[-1].get("h") or 0)
            peak = max(float(trig.get("peak_price") or 0), high)
            invalidation = float(
                (trig.get("timing_block_receipt") or {}).get(
                    "confirmation_price") or 0) * 0.5
            verdict, receipt = detect_pullback_reentry(
                bars, peak, invalidation, cfg)
            update = {"peak_price": peak, "last_price": price,
                      "last_check": {**receipt, "ts": _now_iso()}}
            if verdict == "invalidated":
                update.update(state="INVALIDATED",
                              state_reason=receipt.get("why"),
                              state_ts=_now_iso())
            elif verdict == "reenter":
                child_id = await _emit_child_intent(trig, receipt)
                if child_id:
                    update.update(
                        state="REARMED", state_reason="reaccelerated",
                        state_ts=_now_iso(),
                        rearm_attempt_id=child_id,
                        attempts=int(trig.get("attempts") or 0) + 1,
                        new_confirmation_price=receipt["new_confirmation_price"],
                        new_invalidation_price=receipt["new_invalidation_price"],
                    )
                    acted += 1
                    logger.info(
                        "entry_rearm: REARMED %s — new confirmation %s "
                        "(was blocked at %s), child=%s",
                        trig["symbol"], receipt["new_confirmation_price"],
                        trig.get("block_price"), child_id[:8])
            await db[TRIGGERS].update_one(
                {"trigger_id": trig["trigger_id"]}, {"$set": update})
        except Exception as exc:  # noqa: BLE001
            logger.warning("entry_rearm tick failed for %s: %s",
                           trig.get("symbol"), exc)
    return acted


async def watcher_loop() -> None:
    logger.info("entry_rearm watcher started")
    while True:
        try:
            cfg = await get_rearm_config()
            await _tick()
            await asyncio.sleep(cfg["tick_seconds"])
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("entry_rearm loop error: %s", exc)
            await asyncio.sleep(30)
