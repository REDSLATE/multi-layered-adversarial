"""Fill Cost Capture (2026-06 operator directive, priority 1).

Records ACTUAL production execution economics on every Kraken fill so
the Promotion Gate v2 measured-cost feed grounds itself in what was
actually paid instead of assumptions:

  signal price · submitted limit · actual fill price · qty/notional ·
  actual exchange fee (+ currency) · maker/taker · slippage vs signal ·
  slippage vs submitted limit · intent linkage

Legs are recorded at order submit (status=pending) and resolved by a
rate-limit-friendly background loop that calls Kraken QueryOrders for
fee + executed volume + average price. Entry/exit legs are FIFO-paired
per symbol so EXACT realized round-trip cost is available (used by the
gate's PairedCostFeed instead of 2×avg-leg estimation).

Observe-only: nothing here gates, sizes or blocks trades.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger("risedual.execution_costs")

COLLECTION = "execution_fill_costs"
_MAKER_STYLES = {"post_only_limit", "recovery_ladder"}


def _f(v) -> Optional[float]:
    try:
        f = float(v)
        return f if f > 0 else None
    except (TypeError, ValueError):
        return None


def _signal_price(intent: dict) -> Optional[float]:
    for key in ("price_at_signal", "price", "entry_price"):
        v = intent.get(key) or (intent.get("evidence") or {}).get(key)
        p = _f(v)
        if p:
            return p
    return None


async def record_fill_leg(intent: dict, order: dict,
                          notional_usd: float) -> None:
    """Best-effort leg record at submit time. Fee/fill resolve later."""
    try:
        from db import db  # noqa: WPS433
        order_id = order.get("order_id")
        if not order_id:
            return
        style = str(order.get("order_style") or "")
        side = (order.get("side") or intent.get("action") or "BUY").upper()
        side = "BUY" if side in ("BUY", "COVER") else "SELL"
        await db[COLLECTION].update_one(
            {"_id": str(order_id)},
            {"$set": {
                "intent_id": intent.get("intent_id"),
                "symbol": intent.get("symbol") or order.get("canonical"),
                "lane": (intent.get("lane") or "crypto").lower(),
                "side": side,
                "ts": datetime.now(timezone.utc).isoformat(),
                "signal_price": _signal_price(intent),
                "submitted_limit_price": _f(order.get("limit_price")),
                "order_style": style or "market",
                "liquidity": "maker" if style in _MAKER_STYLES else "taker",
                "qty_submitted": _f(order.get("volume_base")),
                "notional_usd": round(float(notional_usd), 2),
                "broker": order.get("broker") or "kraken",
                "status": "pending",
                "resolve_attempts": 0,
            }},
            upsert=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("fill-cost leg record failed: %s", exc)


def _slip_pct(side: str, fill: float, ref: Optional[float]) -> Optional[float]:
    if not ref or ref <= 0 or fill <= 0:
        return None
    if side == "BUY":
        return round((fill - ref) / ref * 100.0, 6)
    return round((ref - fill) / ref * 100.0, 6)


async def resolve_pending(max_lookups: int = 8) -> dict:
    """Resolve pending legs via Kraken QueryOrders (fee, vol_exec,
    avg price). Capped per cycle — Kraken rate limits are the reason
    this is a slow loop, not a hot path."""
    from db import db  # noqa: WPS433
    from shared.crypto.broker_adapter import get_kraken_adapter  # noqa: WPS433
    cut = (datetime.now(timezone.utc) - timedelta(seconds=90)).isoformat()
    rows = await db[COLLECTION].find(
        {"status": "pending", "ts": {"$lte": cut}},
    ).sort("ts", -1).max_time_ms(5000).to_list(max_lookups)
    if not rows:
        return {"resolved": 0, "pending": 0}
    adapter = await get_kraken_adapter()
    if adapter is None:
        return {"skipped": "no kraken adapter"}
    stats = {"resolved": 0, "unfilled": 0, "errors": 0}
    for r in rows:
        try:
            info = await adapter.get_order(str(r["_id"]))
        except Exception as exc:  # noqa: BLE001
            attempts = int(r.get("resolve_attempts") or 0) + 1
            await db[COLLECTION].update_one(
                {"_id": r["_id"]},
                {"$set": {"resolve_attempts": attempts,
                          "status": ("unresolvable" if attempts >= 5
                                     else "pending"),
                          "last_error": str(exc)[:200]}})
            stats["errors"] += 1
            continue
        raw = info.get("raw") or {}
        vol_exec = _f(raw.get("vol_exec"))
        fill_price = _f(raw.get("price")) or _f(info.get("filled_avg_price"))
        status = (info.get("status") or "").upper()
        if not vol_exec or not fill_price:
            terminal = status in ("CANCELED", "CANCELLED", "EXPIRED",
                                  "REJECTED", "FAILED")
            await db[COLLECTION].update_one(
                {"_id": r["_id"]},
                {"$set": {"status": "unfilled" if terminal else "pending",
                          "resolve_attempts":
                          int(r.get("resolve_attempts") or 0) + 1}})
            stats["unfilled"] += 1 if terminal else 0
            continue
        fee = None
        try:
            fee = float(raw.get("fee")) if raw.get("fee") is not None else None
        except (TypeError, ValueError):
            fee = None
        gross_quote = fill_price * vol_exec
        fee_pct = (round(fee / gross_quote * 100.0, 6)
                   if fee is not None and gross_quote > 0 else None)
        side = r.get("side") or "BUY"
        slip_sig = _slip_pct(side, fill_price, r.get("signal_price"))
        slip_lim = _slip_pct(side, fill_price, r.get("submitted_limit_price"))
        ref_slip = slip_sig if slip_sig is not None else slip_lim
        eff = (max(0.0, (fee_pct or 0.0) + (ref_slip or 0.0))
               if fee_pct is not None or ref_slip is not None else None)
        await db[COLLECTION].update_one(
            {"_id": r["_id"]},
            {"$set": {
                "status": "resolved",
                "resolved_at": datetime.now(timezone.utc).isoformat(),
                "fill_price": fill_price,
                "qty_filled": vol_exec,
                "fee_quote": fee,
                "fee_currency": "quote(USD)",
                "fee_pct": fee_pct,
                "slippage_vs_signal_pct": slip_sig,
                "slippage_vs_limit_pct": slip_lim,
                "effective_leg_cost_pct": round(eff, 6) if eff is not None else None,
            }})
        stats["resolved"] += 1
    return stats


def pair_round_trips(rows: list[dict]) -> list[dict]:
    """FIFO-pair resolved BUY→SELL legs per symbol → EXACT realized
    round-trip cost + realized return. `rows` chronological."""
    open_buys: dict[str, list[dict]] = {}
    pairs = []
    for r in rows:
        if r.get("status") != "resolved" or not r.get("fill_price"):
            continue
        sym = r.get("symbol") or "?"
        if (r.get("side") or "").upper() == "BUY":
            open_buys.setdefault(sym, []).append(r)
            continue
        buys = open_buys.get(sym) or []
        if not buys:
            continue
        b = buys.pop(0)
        b_cost = b.get("effective_leg_cost_pct")
        s_cost = r.get("effective_leg_cost_pct")
        rt_cost = (round(b_cost + s_cost, 6)
                   if b_cost is not None and s_cost is not None else None)
        gross_ret = round((r["fill_price"] - b["fill_price"])
                          / b["fill_price"] * 100.0, 6)
        fees = (b.get("fee_pct") or 0.0) + (r.get("fee_pct") or 0.0)
        pairs.append({
            "symbol": sym,
            "buy_ts": b.get("ts"), "sell_ts": r.get("ts"),
            "buy_intent": b.get("intent_id"), "sell_intent": r.get("intent_id"),
            "round_trip_cost_pct": rt_cost,
            "gross_return_pct": gross_ret,
            "net_return_pct": round(gross_ret - fees, 6),
        })
    return pairs


async def worker_loop() -> None:
    logger.info("fill-cost capture resolver started")
    while True:
        try:
            await asyncio.sleep(180)
            await resolve_pending()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("execution_costs loop error: %s", exc)
