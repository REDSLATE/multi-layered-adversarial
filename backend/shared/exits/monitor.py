"""Position Exit Monitor — autonomous position closure (2026-07-22).

Operator directive: "It does not decide whether a trade deserves
entry; it makes sure an accepted trade has a complete lifecycle."

Doctrine:
  * Broker reconciliation LEADS every cycle. The broker's confirmed
    positions are the only truth; the local plan table follows them.
    (The legacy `shared/risk/position_monitor.py` watched
    `shared_live_positions`, which nothing writes to since the
    direct-execute path was deleted — it evaluated an empty list
    forever. This module supersedes it.)
  * Exit-plan priority: brain-authored target/stop on the executed
    intent → lane-specific operator defaults → never an unbounded
    position. Levels are FIXED at adoption (entry_price basis) and
    never recomputed from later quotes — no bracket drift.
  * Exit-order policy (not blind MARKET):
        stop_loss  → MARKET               (certainty of exit)
        take_profit→ marketable LIMIT     (don't cross a wide spread)
        max_hold   → marketable LIMIT
    LIMIT exits escalate to MARKET when unfilled after
    `escalate_after_s`. Webull exits are ALWAYS MARKET — the broker
    prohibits LIMIT on fractional (<1 share) quantities and the
    $5-10 pilot band means every equity position is fractional.
  * Protections: atomic trigger reservation (one active exit order
    per position), broker-held qty as the sell ceiling, partial-fill
    handling via re-reconciliation, restart recovery (plans persist
    in Mongo), stale-quote rejection, per-lane enable switches,
    permanent receipts in `shared_exit_receipts` (not retention-swept).
  * Max-hold clock starts at plan adoption — positions are adopted
    within one monitor tick (~20s) of the first confirmed fill.
  * Scope day-1: LONG spot/cash positions. Kraken margin positions
    (OpenPositions) are surfaced as `unmanaged_margin` in status but
    not auto-exited.
"""
from __future__ import annotations

import asyncio
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from db import db

logger = logging.getLogger("risedual.exit_monitor")

EXIT_PLANS = "shared_exit_plans"
EXIT_RECEIPTS = "shared_exit_receipts"

INTERVAL_SEC = float(os.environ.get("EXIT_MONITOR_INTERVAL_SEC", "20"))
MAX_EXIT_ATTEMPTS = int(os.environ.get("EXIT_MONITOR_MAX_ATTEMPTS", "5"))
MARKETABLE_LIMIT_BPS = float(os.environ.get("EXIT_MARKETABLE_LIMIT_BPS", "15"))
BRAIN_LEVEL_LOOKBACK_H = 72

# Kraken legacy asset-code aliases (Balance keys → base symbol).
_KRAKEN_ASSET_ALIASES = {
    "XXBT": "BTC", "XBT": "BTC", "XETH": "ETH", "XXRP": "XRP",
    "XXLM": "XLM", "XXDG": "DOGE", "XDG": "DOGE", "XLTC": "LTC",
    "XXMR": "XMR", "XETC": "ETC", "XZEC": "ZEC", "XREP": "REP",
}
_KRAKEN_CASH_ASSETS = {"ZUSD", "USD", "ZEUR", "EUR", "ZGBP", "GBP", "USDT", "USDC"}

_state: dict[str, Any] = {
    "running": False, "task": None, "started_at": None,
    "last_tick_at": None, "last_tick_summary": None,
    "tick_count": 0, "exits_submitted": 0, "errors": 0,
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: Optional[datetime] = None) -> str:
    return (dt or _now()).isoformat()


async def _receipt(row: dict) -> None:
    """Permanent audit row — committed to the local SQLite outbox
    first (2026-07-23 durable-outbox doctrine); the write-behind
    writer mirrors it to Atlas with retry. Falls back to a direct
    Atlas insert only if the local commit itself fails."""
    row.setdefault("ts", _iso())
    try:
        from shared.hotpath import outbox  # noqa: WPS433
        outbox.enqueue(
            "exit_receipt",
            f"{row.get('plan_id', 'na')}-{uuid.uuid4().hex[:8]}",
            row,
        )
        return
    except Exception as exc:  # noqa: BLE001
        logger.warning("exit receipt outbox enqueue failed: %s", exc)
    try:
        await db[EXIT_RECEIPTS].insert_one(dict(row))
    except Exception as exc:  # noqa: BLE001
        logger.warning("exit receipt write failed: %s", exc)


# ── broker position readers (reconciliation source of truth) ───────

async def _equity_positions() -> Optional[list[dict]]:
    """Webull confirmed positions. None = broker unreachable (skip
    lane this tick — NEVER treat fetch failure as 'no positions')."""
    try:
        from shared.broker.webull import get_webull_adapter  # noqa: WPS433
        adapter = await get_webull_adapter()
        if adapter is None:
            return None
        rows = await adapter.list_positions()
    except Exception as exc:  # noqa: BLE001
        logger.warning("exit_monitor equity position fetch failed: %s", exc)
        return None
    out = []
    for p in rows:
        qty = float(p.get("qty") or 0)
        if qty <= 0:
            continue
        out.append({
            "symbol": (p.get("symbol") or "").upper(),
            "qty": qty,
            "entry_price": float(p.get("avg_entry_price") or 0) or None,
            "current_price": float(p.get("current_price") or 0) or None,
        })
    return out


def _normalize_kraken_asset(code: str) -> Optional[str]:
    c = (code or "").upper().split(".")[0]  # strip .S staking suffixes
    if c in _KRAKEN_CASH_ASSETS:
        return None
    return _KRAKEN_ASSET_ALIASES.get(c, c)


async def _crypto_positions() -> Optional[tuple[list[dict], int]]:
    """Kraken spot holdings from Balance + count of unmanaged margin
    positions. None = broker unreachable."""
    try:
        from shared.crypto.broker_adapter import get_kraken_adapter  # noqa: WPS433
        from shared.crypto.kraken import call_private  # noqa: WPS433
        adapter = await get_kraken_adapter()
        if adapter is None:
            return None
        balances = await call_private(
            "/0/private/Balance", adapter.public_key, adapter.private_key, {},
        )
        margin = await adapter.list_positions()
    except Exception as exc:  # noqa: BLE001
        logger.warning("exit_monitor crypto position fetch failed: %s", exc)
        return None

    out = []
    for code, raw in (balances or {}).items():
        base = _normalize_kraken_asset(code)
        if base is None:
            continue
        try:
            qty = float(raw)
        except (TypeError, ValueError):
            continue
        if qty <= 0:
            continue
        symbol = f"{base}/USD"
        price = await _crypto_price(symbol)
        if price is None:
            continue  # stale-quote rejection: unpriceable → skip
        if qty * price < 1.0:
            continue  # dust
        out.append({
            "symbol": symbol,
            "qty": qty,
            "entry_price": None,  # Balance carries no cost basis
            "current_price": price,
        })
    return out, len(margin or [])


async def _crypto_price(symbol: str) -> Optional[float]:
    try:
        from shared.crypto.kraken import to_kraken_pair  # noqa: WPS433
        from shared.crypto.broker_adapter import _ticker_price  # noqa: WPS433
        p = await _ticker_price(to_kraken_pair(symbol))
        return p if p and p > 0 else None
    except Exception:  # noqa: BLE001
        return None


# ── plan adoption / levels ──────────────────────────────────────────

async def _origin_intent(symbol: str, lane: str) -> Optional[dict]:
    """Most recent executed intent for attribution (brain = `stack`)."""
    since = (_now() - timedelta(hours=BRAIN_LEVEL_LOOKBACK_H)).isoformat()
    try:
        return await db["shared_intents"].find_one(
            {"symbol": symbol, "lane": lane, "executed": True,
             "ingest_ts": {"$gte": since}},
            {"intent_id": 1, "stack": 1},
            sort=[("ingest_ts", -1)],
        )
    except Exception:  # noqa: BLE001
        return None


async def _brain_levels(symbol: str, lane: str) -> Optional[tuple[float, float]]:
    """(target, stop) from the most recent executed intent that
    carried a bracket thesis. Exact stored prices — no recompute."""
    since = (_now() - timedelta(hours=BRAIN_LEVEL_LOOKBACK_H)).isoformat()
    try:
        doc = await db["shared_intents"].find_one(
            {
                "symbol": symbol, "lane": lane, "executed": True,
                "ingest_ts": {"$gte": since},
                "target_price": {"$gt": 0}, "stop_price": {"$gt": 0},
            },
            sort=[("ingest_ts", -1)],
        )
    except Exception:  # noqa: BLE001
        return None
    if not doc:
        return None
    try:
        return float(doc["target_price"]), float(doc["stop_price"])
    except (TypeError, ValueError, KeyError):
        return None


async def _entry_price_fallback(symbol: str, lane: str) -> Optional[float]:
    """Best confirmed fill price when the broker doesn't supply cost
    basis (Kraken Balance)."""
    since = (_now() - timedelta(hours=BRAIN_LEVEL_LOOKBACK_H)).isoformat()
    try:
        doc = await db["executions"].find_one(
            {"symbol": symbol, "lane": lane, "ok": True, "ts": {"$gte": since}},
            sort=[("ts", -1)],
        )
        if doc:
            for k in ("filled_avg_price", "fill_price", "price"):
                v = doc.get(k)
                if v and float(v) > 0:
                    return float(v)
    except Exception:  # noqa: BLE001
        pass
    return None


async def _adopt(lane: str, pos: dict, policy: dict) -> dict:
    """Create an exit plan for a broker position that lacks one.
    Levels FIXED here — never recomputed later."""
    symbol = pos["symbol"]
    entry = pos.get("entry_price")
    if not entry:
        entry = await _entry_price_fallback(symbol, lane)
    if not entry:
        entry = pos.get("current_price")  # last resort; documented
    entry = float(entry)

    brain = await _brain_levels(symbol, lane)
    origin = await _origin_intent(symbol, lane)
    lane_p = policy[lane]
    if brain and brain[1] < entry < brain[0]:
        target, stop, source = brain[0], brain[1], "brain"
    else:
        stop = entry * (1.0 - lane_p["sl_pct"] / 100.0)
        target = entry * (1.0 + lane_p["tp_pct"] / 100.0)
        source = "lane_default"

    plan = {
        "plan_id": uuid.uuid4().hex,
        "lane": lane,
        "symbol": symbol,
        "status": "active",
        "entry_price": entry,
        "stop_price": stop,
        "target_price": target,
        "levels_source": source,
        "origin_intent_id": (origin or {}).get("intent_id"),
        "origin_stack": (origin or {}).get("stack"),
        "qty_held": float(pos["qty"]),
        "adopted_at": _iso(),
        "max_hold_until": _iso(_now() + timedelta(hours=lane_p["max_hold_h"])),
        "exit_order": None,
        "exit_reason": None,
        "attempts": 0,
    }
    await db[EXIT_PLANS].insert_one(dict(plan))
    logger.info(
        "exit_monitor ADOPTED %s %s qty=%.8f entry=%.4f stop=%.4f "
        "target=%.4f source=%s hold_until=%s",
        lane, symbol, plan["qty_held"], entry, stop, target, source,
        plan["max_hold_until"],
    )
    return plan


async def _reconcile(lane: str, positions: list[dict], policy: dict) -> list[dict]:
    """Broker positions → plan table. Returns the live plans (active +
    exiting) with `qty_held` refreshed from the broker."""
    by_symbol = {p["symbol"]: p for p in positions}
    live: list[dict] = []
    seen: set[str] = set()

    async for plan in db[EXIT_PLANS].find(
        {"lane": lane, "status": {"$in": ["active", "exiting"]}},
    ):
        sym = plan["symbol"]
        pos = by_symbol.get(sym)
        if pos is None:
            # Position gone at broker → lifecycle complete.
            detail = (
                "exit_order_filled" if plan["status"] == "exiting"
                else "position_closed_externally"
            )
            await db[EXIT_PLANS].update_one(
                {"plan_id": plan["plan_id"]},
                {"$set": {
                    "status": "closed", "closed_at": _iso(),
                    "close_detail": detail,
                }},
            )
            # Realized outcome → durable local outbox first (2026-07-23);
            # the writer applies it to Atlas (ledger + DAWE fold) with
            # retry. Direct call only if the local commit fails.
            try:
                from shared.hotpath import outbox  # noqa: WPS433
                payload = {
                    k: v for k, v in plan.items() if not k.startswith("_")
                }
                payload["close_detail"] = detail
                outbox.enqueue("exit_outcome", plan["plan_id"], payload)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "exit outcome outbox enqueue failed plan=%s: %s",
                    plan["plan_id"], exc,
                )
                from shared.exits.outcomes import record_outcome  # noqa: WPS433
                await record_outcome({**plan, "close_detail": detail})
            if plan["status"] == "exiting":
                await _receipt({
                    "event": "exit_complete", "plan_id": plan["plan_id"],
                    "lane": lane, "symbol": sym,
                    "trigger": plan.get("exit_reason"),
                    "order": plan.get("exit_order"),
                })
            continue
        seen.add(sym)
        if abs(float(pos["qty"]) - float(plan.get("qty_held") or 0)) > 1e-9:
            await db[EXIT_PLANS].update_one(
                {"plan_id": plan["plan_id"]},
                {"$set": {"qty_held": float(pos["qty"])}},
            )
            plan["qty_held"] = float(pos["qty"])
        plan["_pos"] = pos
        live.append(plan)

    for sym, pos in by_symbol.items():
        if sym not in seen:
            plan = await _adopt(lane, pos, policy)
            plan["_pos"] = pos
            live.append(plan)
    return live


# ── trigger evaluation / execution ──────────────────────────────────

def _trigger_for(plan: dict, price: float) -> Optional[str]:
    if price <= float(plan["stop_price"]):
        return "stop_loss"
    if price >= float(plan["target_price"]):
        return "take_profit"
    if _iso() > str(plan["max_hold_until"]):
        return "max_hold"
    return None


async def _reserve(plan_id: str, reason: str) -> bool:
    """Atomic trigger reservation — one active exit order per position."""
    res = await db[EXIT_PLANS].find_one_and_update(
        {"plan_id": plan_id, "status": "active"},
        {"$set": {
            "status": "exiting", "exit_reason": reason,
            "reserved_at": _iso(),
        }},
    )
    return res is not None


def _mint_exit_receipt(lane: str, symbol: str, qty: float) -> Optional[dict]:
    """MC execution receipt for the Kraken adapter's bypass guard."""
    try:
        from shared.broker_router import _mint_and_verify_mc_receipt  # noqa: WPS433
        from shared.broker_symbol_resolver import AssetKey  # noqa: WPS433
        base = symbol.split("/")[0]
        asset = AssetKey(
            canonical=symbol, lane=lane, base=base,
            quote="USD" if lane == "crypto" else None,
        )
        check = _mint_and_verify_mc_receipt(
            intent={"stack": "exit_monitor", "confidence": 1.0,
                    "room_id": "exit_monitor_room"},
            asset=asset, side="SELL", notional_usd=0.0,
        )
        return check.get("receipt")
    except Exception as exc:  # noqa: BLE001
        logger.warning("exit receipt mint failed %s: %s", symbol, exc)
        return None


async def _submit_exit(plan: dict, price: float, *, force_market: bool = False) -> None:
    """Submit the closing order per the exit-order policy. Broker-held
    qty is the ceiling (already refreshed in reconcile)."""
    lane, symbol = plan["lane"], plan["symbol"]
    qty = float(plan["qty_held"])
    trigger = plan["exit_reason"]
    use_market = force_market or trigger == "stop_loss" or lane == "equity"
    order: Optional[dict] = None
    error: Optional[str] = None
    kind = "market"
    limit_price = None
    coid = f"exit-{plan['plan_id'][:12]}-{int(plan.get('attempts') or 0)}"

    try:
        if lane == "equity":
            from shared.broker.webull import get_webull_adapter  # noqa: WPS433
            adapter = await get_webull_adapter()
            if adapter is None:
                raise RuntimeError("webull adapter unavailable")
            order = await adapter.submit_close_market(
                symbol, qty, client_order_id=coid,
            )
        else:
            from shared.crypto.broker_adapter import get_kraken_adapter  # noqa: WPS433
            adapter = await get_kraken_adapter()
            if adapter is None:
                raise RuntimeError("kraken adapter unavailable")
            receipt = _mint_exit_receipt(lane, symbol, qty)
            if not receipt:
                raise RuntimeError("mc receipt mint failed")
            if use_market:
                order = await adapter.submit_market_order(
                    symbol, qty=qty, side="SELL",
                    client_order_id=coid, mc_receipt=receipt,
                )
            else:
                kind = "limit"
                limit_price = price * (1.0 - MARKETABLE_LIMIT_BPS / 10_000.0)
                order = await adapter.submit_limit_order(
                    symbol, qty=qty, limit_price=limit_price, side="SELL",
                    client_order_id=coid, mc_receipt=receipt,
                )
    except Exception as exc:  # noqa: BLE001
        error = str(exc)[:400]

    attempts = int(plan.get("attempts") or 0) + 1
    update: dict = {"attempts": attempts, "exit_price_est": price}
    if order is not None:
        update["exit_order"] = {
            "order_id": order.get("order_id"),
            "client_order_id": coid,
            "kind": kind,
            "limit_price": limit_price,
            "qty": qty,
            "submitted_at": _iso(),
        }
        update["last_error"] = None
        _state["exits_submitted"] += 1
    else:
        update["last_error"] = error
        if attempts >= MAX_EXIT_ATTEMPTS:
            update["status"] = "error"
    await db[EXIT_PLANS].update_one(
        {"plan_id": plan["plan_id"]}, {"$set": update},
    )
    await _receipt({
        "event": "exit_submit" if order else "exit_submit_failed",
        "plan_id": plan["plan_id"], "lane": lane, "symbol": symbol,
        "trigger": trigger, "order_kind": kind, "qty": qty,
        "price_at_trigger": price, "limit_price": limit_price,
        "order_id": (order or {}).get("order_id"),
        "attempt": attempts, "error": error,
    })
    log = logger.info if order else logger.error
    log(
        "exit_monitor SUBMIT %s %s %s trigger=%s kind=%s qty=%.8f "
        "price=%.4f attempt=%d %s",
        "OK" if order else "FAILED", lane, symbol, trigger, kind, qty,
        price, attempts, error or "",
    )


async def _tend_exiting(plan: dict, price: Optional[float], escalate_after_s: float) -> None:
    """Exit-order reconciliation before any retry: escalate stale
    LIMIT exits to MARKET; re-submit when a prior attempt failed."""
    eo = plan.get("exit_order")
    if eo is None:
        # Reservation exists but no live order (submit failed / crash
        # between reserve+submit → restart recovery lands here).
        if int(plan.get("attempts") or 0) < MAX_EXIT_ATTEMPTS and price:
            await _submit_exit(plan, price, force_market=True)
        return
    submitted_at = eo.get("submitted_at") or plan.get("reserved_at") or _iso()
    try:
        age_s = (_now() - datetime.fromisoformat(submitted_at)).total_seconds()
    except ValueError:
        age_s = 0.0
    if eo.get("kind") == "limit" and age_s > escalate_after_s:
        # Cancel + escalate to MARKET (certainty over price).
        try:
            from shared.crypto.broker_adapter import get_kraken_adapter  # noqa: WPS433
            adapter = await get_kraken_adapter()
            if adapter is not None and eo.get("order_id"):
                await adapter.cancel_order(eo["order_id"])
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "exit_monitor escalation cancel failed %s: %s",
                plan["symbol"], exc,
            )
        await _receipt({
            "event": "exit_escalated", "plan_id": plan["plan_id"],
            "lane": plan["lane"], "symbol": plan["symbol"],
            "stale_order_id": eo.get("order_id"), "age_s": round(age_s, 1),
        })
        await db[EXIT_PLANS].update_one(
            {"plan_id": plan["plan_id"]}, {"$set": {"exit_order": None}},
        )
        if price:
            plan["exit_order"] = None
            await _submit_exit(plan, price, force_market=True)


# ── tick ────────────────────────────────────────────────────────────

async def run_once() -> dict:
    from shared.exits.policy import get_policy  # noqa: WPS433
    policy = await get_policy()
    summary: dict = {"started_at": _iso(), "lanes": {}}

    for lane in ("equity", "crypto"):
        lane_sum: dict = {"enabled": policy[lane]["enabled"]}
        summary["lanes"][lane] = lane_sum
        if not policy[lane]["enabled"]:
            continue

        if lane == "equity":
            try:
                from shared.market_hours import is_equity_rth  # noqa: WPS433
                if not is_equity_rth():
                    lane_sum["skipped"] = "outside_rth"
                    continue
            except Exception:  # noqa: BLE001
                pass
            positions = await _equity_positions()
            unmanaged_margin = 0
        else:
            res = await _crypto_positions()
            positions, unmanaged_margin = res if res is not None else (None, 0)

        if positions is None:
            lane_sum["skipped"] = "broker_unreachable"
            continue
        lane_sum["broker_positions"] = len(positions)
        lane_sum["unmanaged_margin"] = unmanaged_margin

        plans = await _reconcile(lane, positions, policy)
        lane_sum["plans"] = len(plans)
        triggered = 0
        for plan in plans:
            pos = plan.get("_pos") or {}
            price = pos.get("current_price")
            if lane == "crypto" and not price:
                price = await _crypto_price(plan["symbol"])
            if plan["status"] == "exiting":
                await _tend_exiting(plan, price, policy["escalate_after_s"])
                continue
            if not price or price <= 0:
                continue  # stale-quote rejection
            trig = _trigger_for(plan, float(price))
            if trig is None:
                continue
            if await _reserve(plan["plan_id"], trig):
                plan["exit_reason"] = trig
                await _submit_exit(plan, float(price))
                triggered += 1
        lane_sum["triggered"] = triggered

    summary["finished_at"] = _iso()
    _state["last_tick_at"] = summary["finished_at"]
    _state["last_tick_summary"] = summary
    _state["tick_count"] += 1
    return summary


async def close_now(plan_id: str) -> dict:
    """Manual CLOSE NOW — operator override, always MARKET."""
    plan = await db[EXIT_PLANS].find_one({"plan_id": plan_id})
    if not plan:
        return {"ok": False, "error": "plan not found"}
    if plan["status"] not in ("active", "exiting"):
        return {"ok": False, "error": f"plan status is {plan['status']}"}
    if plan["status"] == "active" and not await _reserve(plan_id, "manual_close"):
        return {"ok": False, "error": "reservation lost (already exiting)"}
    plan["exit_reason"] = plan.get("exit_reason") or "manual_close"
    price = (
        await _crypto_price(plan["symbol"])
        if plan["lane"] == "crypto" else None
    ) or float(plan.get("entry_price") or 0)
    await _submit_exit(plan, price, force_market=True)
    fresh = await db[EXIT_PLANS].find_one(
        {"plan_id": plan_id}, {"_id": 0, "last_error": 1, "exit_order": 1},
    )
    ok = bool((fresh or {}).get("exit_order"))
    return {"ok": ok, "error": (fresh or {}).get("last_error")}


# ── loop / lifecycle ────────────────────────────────────────────────

async def _loop() -> None:
    logger.info("exit_monitor loop start interval=%.0fs", INTERVAL_SEC)
    try:
        await db[EXIT_PLANS].create_index([("status", 1), ("lane", 1)])
        await db[EXIT_PLANS].create_index("plan_id", unique=True)
    except Exception:  # noqa: BLE001
        pass
    while True:
        try:
            await run_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            _state["errors"] += 1
            logger.exception("exit_monitor tick failed: %s", exc)
        await asyncio.sleep(INTERVAL_SEC)


def start_if_enabled() -> None:
    if (os.environ.get("EXIT_MONITOR_ENABLED") or "true").strip().lower() in (
        "0", "false", "no", "off",
    ):
        logger.info("exit_monitor disabled via EXIT_MONITOR_ENABLED")
        return
    if _state.get("running"):
        return
    task = asyncio.get_event_loop().create_task(_loop(), name="exit_monitor_loop")
    _state.update(running=True, task=task, started_at=_iso())
    logger.info("exit_monitor started")


async def stop() -> None:
    task = _state.get("task")
    if task and not task.done():
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    _state.update(running=False, task=None)


def get_status() -> dict:
    return {
        "running": _state.get("running", False),
        "interval_sec": INTERVAL_SEC,
        "started_at": _state.get("started_at"),
        "last_tick_at": _state.get("last_tick_at"),
        "last_tick_summary": _state.get("last_tick_summary"),
        "tick_count": _state.get("tick_count", 0),
        "exits_submitted": _state.get("exits_submitted", 0),
        "errors": _state.get("errors", 0),
    }
