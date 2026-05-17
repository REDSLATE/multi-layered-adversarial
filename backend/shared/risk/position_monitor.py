"""Position Monitor scheduler — periodic risk-guard evaluation.

Doctrine (2026-02-17):
    Executors enter. Lifecycle guards exit. Brains advise. RoadGuard
    enforces.

    This module is the RoadGuard automation: every N seconds it walks
    every open position and evaluates the deterministic risk guards in
    strict priority order:

        1. StopLoss       — capital protection comes first
        2. TakeProfit     — lock the win when the target is hit
        3. TrailingStop   — give back too much from the peak → exit
        4. MaxHoldTime    — stale-thesis hygiene, runs last

    The FIRST guard that returns a non-HOLD verdict closes (or reduces)
    the position. Lower-priority guards are not consulted for that
    position on the same tick. This guarantees that a stop-loss never
    races a take-profit on a sudden whipsaw bar.

    Lane discipline: equity positions are routed to `shared.equity.*`
    wrappers, crypto positions to `shared.crypto.*`. The monitor itself
    never imports lane-specific math — only the dispatcher.

    Pricing: equity positions use the Alpaca paper account's
    list_positions() current_price. Crypto pricing is TODO (see
    `_crypto_price_for`); until then, crypto positions skip the
    price-based guards and only MaxHoldTime fires.

    Failure isolation: every position is evaluated in its own try/except
    so one bad row never blocks the rest of the loop.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Optional

from db import db
from namespaces import (
    RISK_MONITOR_EVALUATIONS,
    SHARED_LIVE_POSITIONS,
)


logger = logging.getLogger("risedual.position_monitor")


# ─────────────────────────── config / state ───────────────────────────

# Tuneables (env-overridable so the operator can dial cadence without a deploy).
def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return default


INTERVAL_SECONDS = _env_float("POSITION_MONITOR_INTERVAL_SECONDS", 30.0)
STOP_LOSS_PCT = _env_float("POSITION_MONITOR_STOP_LOSS_PCT", 2.0)
TAKE_PROFIT_PCT = _env_float("POSITION_MONITOR_TAKE_PROFIT_PCT", 3.0)
TRAIL_PCT = _env_float("POSITION_MONITOR_TRAIL_PCT", 1.5)
TRAIL_ACTIVATE_PCT = _env_float("POSITION_MONITOR_TRAIL_ACTIVATE_PCT", 1.0)
MAX_HOLD_MINUTES = _env_float("POSITION_MONITOR_MAX_HOLD_MINUTES", 60.0 * 24.0)
ENABLED = _env_bool("POSITION_MONITOR_ENABLED", True)


# In-process state — read by /admin/risk/monitor/status.
_state: dict = {
    "running": False,
    "task": None,
    "started_at": None,
    "last_tick_at": None,
    "last_tick_summary": None,
    "tick_count": 0,
    "evaluated_count": 0,
    "actions_taken": 0,
    "errors": 0,
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ─────────────────────────── price oracle ───────────────────────────

async def _equity_prices() -> dict[str, float]:
    """Snapshot of current prices for every equity position the broker
    knows about. Returns {symbol_upper: current_price}. Empty dict on
    any failure (the monitor will treat each position as 'no price').
    """
    try:
        from shared.broker.alpaca_routes import get_alpaca_adapter  # noqa: WPS433
        adapter = await get_alpaca_adapter()
        if adapter is None:
            return {}
        positions = await adapter.list_positions()
        out: dict[str, float] = {}
        for p in positions or []:
            sym = (p.get("symbol") or "").upper()
            price = p.get("current_price")
            if sym and price is not None and price > 0:
                out[sym] = float(price)
        return out
    except Exception as e:  # noqa: BLE001
        logger.warning("position_monitor: equity price snapshot failed: %s", e)
        return {}


async def _crypto_prices(symbols: list[str]) -> dict[str, float]:
    """Snapshot of current prices for the supplied crypto symbols using
    Kraken's unauthenticated public `/0/public/Ticker` endpoint.

    Returns `{symbol_upper: last_trade_price}`. Symbols that Kraken
    doesn't recognise (or that fail to parse) are silently dropped — the
    monitor treats them as "no price" and skips price-based guards for
    that position on this tick. Lane-neutral consumer: we don't touch
    `kraken_credentials`, we don't need execution scope, we don't even
    need the credentials doc to exist.
    """
    if not symbols:
        return {}
    try:
        from shared.crypto.kraken import fetch_tickers  # noqa: WPS433
        return await fetch_tickers(symbols)
    except Exception as e:  # noqa: BLE001
        logger.warning("position_monitor: crypto price snapshot failed: %s", e)
        return {}


async def _crypto_price_for(symbol: str) -> Optional[float]:
    """Single-symbol convenience for one-off callers. The hot path
    (the monitor loop) uses `_crypto_prices` to batch every open
    position into a single Kraken request per tick — much kinder to
    their rate limits than a request per position.
    """
    prices = await _crypto_prices([symbol])
    return prices.get((symbol or "").upper().strip())


# ─────────────────────────── evaluation log ───────────────────────────

async def _log_evaluation(row: dict) -> None:
    """Append-only evaluation log so the operator can audit every
    decision the monitor made."""
    try:
        await db[RISK_MONITOR_EVALUATIONS].insert_one(row.copy())
    except Exception as e:  # noqa: BLE001
        logger.warning("position_monitor: eval log write failed: %s", e)


# ─────────────────────────── core evaluation ───────────────────────────

GUARDS_PRIORITY = ("stop_loss", "take_profit", "trailing_stop", "max_hold_time")


def _equity_guard_modules():
    # Lazy import so we never violate lane isolation at module-load time.
    from shared.equity import (  # noqa: WPS433
        max_hold_time as e_mht,
        stop_loss as e_sl,
        take_profit as e_tp,
        trailing_stop as e_ts,
    )
    return {"stop_loss": e_sl, "take_profit": e_tp, "trailing_stop": e_ts, "max_hold_time": e_mht}


def _crypto_guard_modules():
    from shared.crypto import (  # noqa: WPS433
        max_hold_time as c_mht,
        stop_loss as c_sl,
        take_profit as c_tp,
        trailing_stop as c_ts,
    )
    return {"stop_loss": c_sl, "take_profit": c_tp, "trailing_stop": c_ts, "max_hold_time": c_mht}


async def _evaluate_one(
    position: dict, current_price: Optional[float], actor: str,
) -> dict:
    """Walk the four guards in priority order. First non-HOLD wins.
    Returns a summary row that gets written to RISK_MONITOR_EVALUATIONS.
    """
    pid = position["position_id"]
    lane = (position.get("lane") or "").lower()
    if lane == "equity":
        guards = _equity_guard_modules()
    elif lane == "crypto":
        guards = _crypto_guard_modules()
    else:
        return {
            "ts": _now_iso(),
            "position_id": pid,
            "lane": lane or None,
            "skipped": True,
            "skipped_reason": f"unknown lane {lane!r}",
            "fired_guard": None,
        }

    fired_guard = None
    fired_action = None
    fired_reason = None
    holds: list[dict] = []
    enforce_result: Optional[dict] = None

    for guard_name in GUARDS_PRIORITY:
        # Price-dependent guards skip when no price is available.
        if guard_name in ("stop_loss", "take_profit", "trailing_stop") and (
            current_price is None or current_price <= 0
        ):
            holds.append({"guard": guard_name, "action": "SKIP", "reason": "no price"})
            continue

        mod = guards[guard_name]
        try:
            if guard_name == "stop_loss":
                eval_payload = await mod.evaluate_position(
                    position_id=pid, current_price=current_price,
                    stop_loss_pct=STOP_LOSS_PCT,
                )
            elif guard_name == "take_profit":
                eval_payload = await mod.evaluate_position(
                    position_id=pid, current_price=current_price,
                    take_profit_pct=TAKE_PROFIT_PCT,
                )
            elif guard_name == "trailing_stop":
                eval_payload = await mod.evaluate_position(
                    position_id=pid, current_price=current_price,
                    trail_pct=TRAIL_PCT,
                    activate_after_pct=TRAIL_ACTIVATE_PCT,
                    persist_peak=True,
                )
            else:  # max_hold_time
                eval_payload = await mod.evaluate_position(
                    position_id=pid, current_price=current_price,
                    max_hold_minutes=MAX_HOLD_MINUTES,
                )
        except Exception as e:  # noqa: BLE001
            holds.append({"guard": guard_name, "action": "ERROR", "reason": str(e)[:200]})
            continue

        action = (eval_payload.get("verdict") or {}).get("action") or "HOLD"
        if action == "HOLD":
            holds.append({
                "guard": guard_name, "action": "HOLD",
                "reason": (eval_payload.get("verdict") or {}).get("reason", ""),
            })
            continue

        # Non-HOLD — enforce this guard now and stop iterating.
        fired_guard = guard_name
        fired_action = action
        fired_reason = (eval_payload.get("verdict") or {}).get("reason", "")
        try:
            if guard_name == "stop_loss":
                enforce_result = await mod.enforce_position(
                    position_id=pid, current_price=current_price,
                    actor=actor + f" · {guard_name}",
                    stop_loss_pct=STOP_LOSS_PCT,
                )
            elif guard_name == "take_profit":
                enforce_result = await mod.enforce_position(
                    position_id=pid, current_price=current_price,
                    actor=actor + f" · {guard_name}",
                    take_profit_pct=TAKE_PROFIT_PCT,
                )
            elif guard_name == "trailing_stop":
                enforce_result = await mod.enforce_position(
                    position_id=pid, current_price=current_price,
                    actor=actor + f" · {guard_name}",
                    trail_pct=TRAIL_PCT,
                    activate_after_pct=TRAIL_ACTIVATE_PCT,
                )
            else:
                enforce_result = await mod.enforce_position(
                    position_id=pid, current_price=current_price,
                    actor=actor + f" · {guard_name}",
                    max_hold_minutes=MAX_HOLD_MINUTES,
                )
        except Exception as e:  # noqa: BLE001
            return {
                "ts": _now_iso(),
                "position_id": pid,
                "lane": lane,
                "symbol": position.get("symbol"),
                "current_price": current_price,
                "fired_guard": guard_name,
                "fired_action": action,
                "fired_reason": fired_reason,
                "enforce_error": str(e)[:300],
                "holds": holds,
            }
        break

    return {
        "ts": _now_iso(),
        "position_id": pid,
        "lane": lane,
        "symbol": position.get("symbol"),
        "current_price": current_price,
        "fired_guard": fired_guard,
        "fired_action": fired_action,
        "fired_reason": fired_reason,
        "holds": holds if fired_guard is None else holds,
        "enforce": {
            "acted": bool(enforce_result and enforce_result.get("acted")),
            "action": (enforce_result or {}).get("action"),
        } if enforce_result else None,
    }


# ─────────────────────────── tick / loop ───────────────────────────

async def run_once(actor: str = "position_monitor") -> dict:
    """One full tick — evaluate every open / managing position once.
    Safe to call manually via /admin/risk/monitor/run-once."""
    started = _now_iso()
    open_positions = await db[SHARED_LIVE_POSITIONS].find(
        {"state": {"$in": ["open", "managing"]}}, {"_id": 0},
    ).to_list(500)

    # Build the equity-price snapshot once per tick (one Alpaca call).
    equity_prices = await _equity_prices()
    # Build the crypto-price snapshot once per tick (one Kraken Ticker
    # call for every crypto symbol currently open). Far gentler on
    # Kraken's rate limits than calling per-position.
    crypto_symbols = sorted({
        (p.get("symbol") or "").upper()
        for p in open_positions
        if (p.get("lane") or "").lower() == "crypto" and p.get("symbol")
    })
    crypto_prices = await _crypto_prices(crypto_symbols) if crypto_symbols else {}

    evaluated = 0
    actions = 0
    errors = 0
    results: list[dict] = []
    for pos in open_positions:
        symbol = (pos.get("symbol") or "").upper()
        lane = (pos.get("lane") or "").lower()
        if lane == "equity":
            current_price = equity_prices.get(symbol)
        elif lane == "crypto":
            current_price = crypto_prices.get(symbol)
        else:
            current_price = None

        try:
            row = await _evaluate_one(pos, current_price, actor)
        except Exception as e:  # noqa: BLE001
            row = {
                "ts": _now_iso(),
                "position_id": pos.get("position_id"),
                "lane": lane,
                "symbol": symbol,
                "current_price": current_price,
                "fatal_error": str(e)[:300],
                "fired_guard": None,
            }
            errors += 1

        await _log_evaluation(row)
        evaluated += 1
        if row.get("fired_guard"):
            actions += 1
        results.append(row)

    summary = {
        "started_at": started,
        "finished_at": _now_iso(),
        "open_positions": len(open_positions),
        "evaluated": evaluated,
        "actions_taken": actions,
        "errors": errors,
        "equity_prices_seen": len(equity_prices),
        "crypto_prices_seen": len(crypto_prices),
        "crypto_symbols_queried": crypto_symbols,
    }

    _state["last_tick_at"] = summary["finished_at"]
    _state["last_tick_summary"] = summary
    _state["tick_count"] += 1
    _state["evaluated_count"] += evaluated
    _state["actions_taken"] += actions
    _state["errors"] += errors

    return {"summary": summary, "results": results}


async def _loop() -> None:
    logger.info("position_monitor: loop start (interval=%.1fs)", INTERVAL_SECONDS)
    while True:
        try:
            await run_once(actor="position_monitor_loop")
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            _state["errors"] += 1
            logger.exception("position_monitor: tick failed: %s", e)
        await asyncio.sleep(INTERVAL_SECONDS)


def start_monitor_if_enabled() -> None:
    """Start the background loop unless POSITION_MONITOR_ENABLED=false.
    Idempotent — safe to call from server lifespan repeatedly."""
    if not ENABLED:
        logger.info("position_monitor: disabled via POSITION_MONITOR_ENABLED")
        return
    if _state.get("running"):
        return
    loop = asyncio.get_event_loop()
    task = loop.create_task(_loop(), name="position_monitor_loop")
    _state["running"] = True
    _state["task"] = task
    _state["started_at"] = _now_iso()
    logger.info("position_monitor: started")


async def stop_monitor() -> None:
    task = _state.get("task")
    if task and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001
            pass
    _state["running"] = False
    _state["task"] = None


def get_status() -> dict:
    return {
        "enabled": ENABLED,
        "running": _state.get("running", False),
        "interval_seconds": INTERVAL_SECONDS,
        "started_at": _state.get("started_at"),
        "last_tick_at": _state.get("last_tick_at"),
        "last_tick_summary": _state.get("last_tick_summary"),
        "tick_count": _state.get("tick_count", 0),
        "evaluated_count": _state.get("evaluated_count", 0),
        "actions_taken": _state.get("actions_taken", 0),
        "errors": _state.get("errors", 0),
        "config": {
            "stop_loss_pct": STOP_LOSS_PCT,
            "take_profit_pct": TAKE_PROFIT_PCT,
            "trail_pct": TRAIL_PCT,
            "trail_activate_pct": TRAIL_ACTIVATE_PCT,
            "max_hold_minutes": MAX_HOLD_MINUTES,
        },
        "priority": list(GUARDS_PRIORITY),
        "doctrine": (
            "StopLoss → TakeProfit → TrailingStop → MaxHoldTime. "
            "First non-HOLD verdict closes/reduces the position. "
            "Brain advisory cannot override."
        ),
    }
