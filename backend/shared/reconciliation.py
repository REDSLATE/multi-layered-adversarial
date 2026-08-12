"""Broker-Fill Reconciliation (2026-06 operator directive).

THE BROKER FILL IS THE SOURCE OF TRUTH. Every real fill must become
durable measurement data even if every intermediate application event
was missed. A real trade may NEVER disappear silently from
measurement — it either becomes a completed outcome or an explicit
unresolved reconciliation exception that keeps retrying.

Chain hardened here:
  broker fill → execution receipt → originating intent → paired round
  trip → realized P&L → fees/slippage → outcome record → epoch
  attribution → measured-cost sample → expectancy/promotion metrics

Collections:
  broker_fills_ledger  every broker-confirmed fill, idempotent by
                       broker fill id; carries link status + reason
  trade_outcomes       finalized FIFO-paired round trips with realized
                       P&L, fees, slippage, brain/epoch attribution

Runs on boot and every 10 minutes — a crash, deploy, timeout or
missed callback can never permanently lose a trade. Does NOT loosen
gates or change trading strategy.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger("risedual.reconciliation")

LEDGER = "broker_fills_ledger"
OUTCOMES = "trade_outcomes"
STATE_FLAG = "reconciliation_state"
_EPS = 1e-12

_ALT_TO_BASE = {"XBT": "BTC", "XDG": "DOGE"}


def _kraken_pair_to_symbol(pair: str) -> str:
    from shared.crypto.kraken import _normalise_kraken_pair_key  # noqa: WPS433
    alt = _normalise_kraken_pair_key(str(pair))
    for quote in ("USD", "USDT", "USDC", "EUR"):
        if alt.endswith(quote) and len(alt) > len(quote):
            base = alt[: -len(quote)]
            return f"{_ALT_TO_BASE.get(base, base)}/{quote}"
    return alt


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ───────────────────────── ingestion ─────────────────────────

async def ingest_kraken_fills(full: bool = False) -> dict:
    """TradesHistory → ledger. Idempotent by Kraken trade txid."""
    from db import db  # noqa: WPS433
    from shared.crypto.broker_adapter import get_kraken_adapter  # noqa: WPS433
    from shared.crypto.kraken import call_private  # noqa: WPS433
    adapter = await get_kraken_adapter()
    if adapter is None:
        return {"skipped": "no kraken credentials"}
    params: dict[str, Any] = {}
    if not full:
        last = await db[LEDGER].find_one(
            {"broker": "kraken"}, {"broker_ts": 1}, sort=[("broker_ts", -1)])
        if last and last.get("broker_ts"):
            params["start"] = float(last["broker_ts"]) - 3600  # 1h overlap
    ofs, pages, upserts, total = 0, 0, 0, None
    max_pages = 40 if full else 4
    while pages < max_pages:
        res = await call_private(
            "/0/private/TradesHistory", adapter.public_key,
            adapter.private_key, {**params, "ofs": ofs})
        trades = (res or {}).get("trades") or {}
        total = (res or {}).get("count")
        if not trades:
            break
        for txid, t in trades.items():
            side = "BUY" if str(t.get("type")) == "buy" else "SELL"
            ts_f = float(t.get("time") or 0)
            doc = {
                "broker": "kraken", "lane": "crypto",
                "symbol": _kraken_pair_to_symbol(t.get("pair") or ""),
                "pair_raw": t.get("pair"),
                "side": side,
                "qty": float(t.get("vol") or 0),
                "price": float(t.get("price") or 0),
                "cost_usd": float(t.get("cost") or 0),
                "fee_usd": float(t.get("fee") or 0),
                "maker": bool(t.get("maker")) if "maker" in t else None,
                "order_id": t.get("ordertxid"),
                "broker_ts": ts_f,
                "ts": datetime.fromtimestamp(
                    ts_f, tz=timezone.utc).isoformat() if ts_f else None,
            }
            r = await db[LEDGER].update_one(
                {"_id": f"kraken:{txid}"},
                {"$set": doc,
                 "$setOnInsert": {"link": {"status": "unlinked",
                                           "reason": "new", "attempts": 0},
                                  "ingested_at": _now_iso()}},
                upsert=True)
            upserts += 1 if r.upserted_id else 0
        ofs += len(trades)
        pages += 1
        if total is not None and ofs >= int(total):
            break
        await asyncio.sleep(1.2)  # Kraken rate limit
    return {"broker": "kraken", "pages": pages, "new_fills": upserts,
            "broker_reported_total": total}


async def ingest_webull_fills(full: bool = False) -> dict:
    """Webull v2 order history → ledger. Idempotent by order_id."""
    from db import db  # noqa: WPS433
    try:
        from shared.broker.webull import get_webull_adapter  # noqa: WPS433
        adapter = get_webull_adapter()
        if asyncio.iscoroutine(adapter):
            adapter = await adapter
    except Exception as exc:  # noqa: BLE001
        return {"skipped": f"webull adapter unavailable: {exc}"}
    if adapter is None:
        return {"skipped": "no webull credentials"}
    from datetime import timedelta  # noqa: WPS433
    start = (datetime.now(timezone.utc)
             - timedelta(days=365 if full else 7)).strftime("%Y-%m-%d")
    try:
        rows = await adapter.list_history(start=start)
    except Exception as exc:  # noqa: BLE001
        return {"skipped": f"webull history failed: {exc}"}
    upserts = 0
    for r in rows:
        oid = r.get("order_id")
        if not oid or not r.get("qty") or not r.get("price"):
            continue
        doc = {
            "broker": "webull", "lane": "equity",
            "symbol": r.get("symbol"),
            "side": (r.get("side") or "").upper(),
            "qty": float(r["qty"]), "price": float(r["price"]),
            "cost_usd": float(r.get("notional") or 0),
            "fee_usd": float(r.get("fee") or 0) if r.get("fee") else None,
            "maker": None,
            "order_id": str(oid),
            "client_order_id": r.get("client_order_id"),
            "ts": r.get("filled_at"),
        }
        res = await db[LEDGER].update_one(
            {"_id": f"webull:{oid}:{r.get('filled_at') or ''}"},
            {"$set": doc,
             "$setOnInsert": {"link": {"status": "unlinked",
                                       "reason": "new", "attempts": 0},
                              "ingested_at": _now_iso()}},
            upsert=True)
        upserts += 1 if res.upserted_id else 0
    return {"broker": "webull", "rows": len(rows), "new_fills": upserts}


# ───────────────────────── linkage ─────────────────────────

async def link_fills(max_batch: int = 500) -> dict:
    """Link ledger fills to internal records. NEVER discards — a fill
    that cannot be linked stays in the queue with a reason and is
    retried every cycle (reconciliation does not require the original
    in-memory process to still exist)."""
    from db import db  # noqa: WPS433
    rows = await db[LEDGER].find(
        {"link.status": {"$in": ["unlinked", "retry"]}},
    ).sort("ts", -1).max_time_ms(10000).to_list(max_batch)
    linked = internal_only = 0
    for f in rows:
        oid = f.get("order_id")
        link: dict[str, Any] = {"attempts": int(
            (f.get("link") or {}).get("attempts") or 0) + 1,
            "last_try": _now_iso()}
        ex = None
        candidates = [c for c in (oid, f.get("client_order_id")) if c]
        if candidates:
            ex = await db["executions"].find_one(
                {"$or": [
                    {"broker_response.order_id": {"$in": candidates}},
                    {"broker_response.client_order_id": {"$in": candidates}},
                    {"broker_response.id": {"$in": candidates}},
                    {"broker_order_id": {"$in": candidates}},
                ]},
                {"intent_id": 1, "brain": 1, "stack": 1, "symbol": 1})
        leg = await db["execution_fill_costs"].find_one(
            {"_id": oid}, {"signal_price": 1, "submitted_limit_price": 1,
                           "slippage_vs_signal_pct": 1,
                           "slippage_vs_limit_pct": 1}) if oid else None
        if ex:
            link.update({"status": "linked",
                         "execution_id": str(ex["_id"]),
                         "intent_id": ex.get("intent_id"),
                         "brain": ex.get("brain"),
                         "stack": ex.get("stack"),
                         "leg_id": oid if leg else None,
                         "reason": None})
            linked += 1
        elif leg:
            link.update({"status": "leg_only", "leg_id": oid,
                         "reason": "no_execution_receipt"})
            internal_only += 1
        else:
            link.update({"status": "retry" if link["attempts"] < 10
                         else "unmatched_internal",
                         "reason": "no_internal_record"})
        upd: dict[str, Any] = {"link": {**(f.get("link") or {}), **link}}
        if leg:
            upd["slippage_vs_signal_pct"] = leg.get("slippage_vs_signal_pct")
            upd["slippage_vs_limit_pct"] = leg.get("slippage_vs_limit_pct")
            upd["signal_price"] = leg.get("signal_price")
            upd["submitted_limit_price"] = leg.get("submitted_limit_price")
        await db[LEDGER].update_one({"_id": f["_id"]}, {"$set": upd})
    return {"checked": len(rows), "linked": linked,
            "leg_only": internal_only}


# ───────────────────────── pairing ─────────────────────────

def pair_fills(fills: list[dict]) -> tuple[list[dict], list[dict]]:
    """Deterministic FIFO lot allocation per (lane, symbol) with
    partial fills and multi-leg exits. `fills` chronological.
    Returns (outcomes, orphan_sells)."""
    lots: dict[tuple, list[dict]] = {}
    outcomes, orphans = [], []
    for f in sorted(fills, key=lambda x: (x.get("ts") or "", x["_id"])):
        if f.get("qty", 0) <= 0 or f.get("price", 0) <= 0:
            continue
        key = (f.get("lane"), f.get("symbol"))
        if f.get("side") == "BUY":
            lots.setdefault(key, []).append(
                {"fill": f, "remaining": float(f["qty"])})
            continue
        remaining = float(f["qty"])
        consumed: list[tuple[dict, float]] = []
        q = lots.get(key) or []
        while remaining > _EPS and q:
            lot = q[0]
            take = min(lot["remaining"], remaining)
            consumed.append((lot["fill"], take))
            lot["remaining"] -= take
            remaining -= take
            if lot["remaining"] <= _EPS:
                q.pop(0)
        if not consumed:
            orphans.append(f)
            continue
        qty = sum(t for _, t in consumed)
        entry_avg = sum(b["price"] * t for b, t in consumed) / qty
        entry_fees = sum((b.get("fee_usd") or 0.0) * (t / float(b["qty"]))
                         for b, t in consumed)
        exit_fee = (f.get("fee_usd") or 0.0) * (qty / float(f["qty"]))
        notional = entry_avg * qty
        pnl = (f["price"] - entry_avg) * qty - entry_fees - exit_fee
        fees = entry_fees + exit_fee
        fee_pct = round(fees / notional * 100.0, 6) if notional > 0 else None
        entry = consumed[0][0]
        slip = entry.get("slippage_vs_signal_pct")
        link = entry.get("link") or {}
        outcomes.append({
            "_id": f"rt:{f['_id']}",
            "lane": f.get("lane"), "symbol": f.get("symbol"),
            "qty": round(qty, 10),
            "entry_avg_price": round(entry_avg, 10),
            "exit_price": f["price"],
            "entry_fill_ids": [b["_id"] for b, _ in consumed],
            "exit_fill_id": f["_id"],
            "partial_exit": remaining > _EPS or (qty < float(f["qty"]) - _EPS),
            "entry_ts": entry.get("ts"), "exit_ts": f.get("ts"),
            "holding_s": _holding_s(entry.get("ts"), f.get("ts")),
            "realized_pnl_usd": round(pnl, 6),
            "gross_return_pct": round(
                (f["price"] - entry_avg) / entry_avg * 100.0, 6),
            "net_return_pct": round(pnl / notional * 100.0, 6)
            if notional > 0 else None,
            "fees_usd": round(fees, 6),
            "fee_pct": fee_pct,
            "entry_slippage_vs_signal_pct": slip,
            "round_trip_cost_pct": (round(fee_pct + max(0.0, slip or 0.0), 6)
                                    if fee_pct is not None else None),
            "intent_id": link.get("intent_id"),
            "brain": link.get("brain"), "stack": link.get("stack"),
            "entry_maker": entry.get("maker"),
            "measured_cost_eligible": fee_pct is not None,
        })
    return outcomes, orphans


def _holding_s(t0: Optional[str], t1: Optional[str]) -> Optional[float]:
    try:
        a = datetime.fromisoformat(str(t0).replace("Z", "+00:00"))
        b = datetime.fromisoformat(str(t1).replace("Z", "+00:00"))
        return round((b - a).total_seconds(), 1)
    except Exception:  # noqa: BLE001
        return None


async def finalize_outcomes() -> dict:
    """Pair the full ledger and upsert outcome records (idempotent —
    deterministic ids derived from broker fill ids). Attributes epoch
    by ENTRY timestamp so outcomes land in the epoch whose execution
    build produced them."""
    from db import db  # noqa: WPS433
    from shared.forensics.gate_v2_adapter import (  # noqa: WPS433
        _epoch_for, current_epoch,
    )
    fills = await db[LEDGER].find({}).sort("ts", 1).max_time_ms(
        15000).to_list(10000)
    outcomes, orphans = pair_fills(fills)
    epoch = await current_epoch()
    for o in outcomes:
        o["epoch_id"] = _epoch_for(str(o.get("entry_ts") or ""), epoch)
        o["finalized_at"] = _now_iso()
        # never clobber an operator's manual orphan resolution
        await db[OUTCOMES].update_one(
            {"_id": o["_id"], "resolution": {"$ne": "manual"}},
            {"$set": o}, upsert=False)
        await db[OUTCOMES].update_one(
            {"_id": o["_id"]}, {"$setOnInsert": o}, upsert=True)
    orphan_ids = {f["_id"] for f in orphans}
    for oid in orphan_ids:
        await db[LEDGER].update_one(
            {"_id": oid, "link.exit_orphan": {"$ne": True}},
            {"$set": {"link.exit_orphan": True,
                      "link.orphan_reason":
                      "SELL with no recorded entry lots (pre-history "
                      "or adopted position)"}})
    return {"outcomes": len(outcomes), "orphan_sells": len(orphan_ids)}


# ───────────────────────── counters ─────────────────────────

async def reconciliation_counters() -> dict:
    from db import db  # noqa: WPS433
    total = await db[LEDGER].count_documents({})
    linked = await db[LEDGER].count_documents({"link.status": "linked"})
    leg_only = await db[LEDGER].count_documents({"link.status": "leg_only"})
    unlinked = await db[LEDGER].count_documents(
        {"link.status": {"$in": ["unlinked", "retry",
                                 "unmatched_internal"]}})
    orphan_exits = await db[LEDGER].count_documents(
        {"link.exit_orphan": True, "link.orphan_resolved": {"$ne": True}})
    orphan_resolved = await db[LEDGER].count_documents(
        {"link.orphan_resolved": True})
    outcomes = await db[OUTCOMES].count_documents({})
    eligible = await db[OUTCOMES].count_documents(
        {"measured_cost_eligible": True})
    oldest = await db[LEDGER].find_one(
        {"link.status": {"$in": ["unlinked", "retry"]}},
        {"ts": 1}, sort=[("ts", 1)])
    oldest_age_h = None
    if oldest and oldest.get("ts"):
        try:
            dt = datetime.fromisoformat(
                str(oldest["ts"]).replace("Z", "+00:00"))
            oldest_age_h = round(
                (datetime.now(timezone.utc) - dt).total_seconds() / 3600, 1)
        except Exception:  # noqa: BLE001
            pass
    health = "green"
    reasons = []
    if unlinked > 0:
        health = "amber"
        reasons.append(f"{unlinked} broker fills without internal linkage")
    if oldest_age_h is not None and oldest_age_h > 24:
        health = "red"
        reasons.append(
            f"oldest unresolved fill is {oldest_age_h}h old (>24h)")
    return {
        "broker_fills_total": total,
        "recorded_fills_total": linked + leg_only,
        "unlinked_fills": unlinked,
        "paired_round_trips": outcomes,
        "completed_outcomes": outcomes,
        "measured_cost_eligible": eligible,
        "measured_cost_count": eligible,
        "exit_linkage_miss_count": orphan_exits,
        "orphan_resolved_count": orphan_resolved,
        "reconciliation_oldest_unresolved_age_h": oldest_age_h,
        "health": {"status": health, "reasons": reasons},
    }


async def run_reconciliation(full: bool = False) -> dict:
    """Ingest → link → pair → counters. Boot + every 10 min + on
    demand ("run now" / full backfill)."""
    from db import db  # noqa: WPS433
    report = {"ts": _now_iso(), "full": full}
    report["kraken"] = await ingest_kraken_fills(full=full)
    report["webull"] = await ingest_webull_fills(full=full)
    report["linkage"] = await link_fills()
    report["finalize"] = await finalize_outcomes()
    report["counters"] = await reconciliation_counters()
    await db["runtime_flags"].update_one(
        {"_id": STATE_FLAG},
        {"$set": {"last_run": report["ts"], "last_report": report}},
        upsert=True)
    logger.info("reconciliation run: %s", report["counters"])
    return report


async def resolve_orphan_exit(
    fill_id: str,
    entry_price: float,
    *,
    operator: str,
    entry_ts: Optional[str] = None,
    note: str = "",
    position_id: Optional[str] = None,
    intent_id: Optional[str] = None,
) -> dict:
    """Operator manually matches an orphan SELL to a known adopted
    position by supplying the entry cost basis. Produces a MANUAL
    outcome so the P&L counts in realized reporting — but it is NOT
    measured-cost eligible (entry-side fees unknown by definition)."""
    from db import db  # noqa: WPS433
    from shared.forensics.gate_v2_adapter import (  # noqa: WPS433
        _epoch_for, current_epoch,
    )
    f = await db[LEDGER].find_one({"_id": fill_id})
    if not f:
        raise ValueError(f"unknown ledger fill {fill_id}")
    link = f.get("link") or {}
    if not link.get("exit_orphan"):
        raise ValueError("fill is not an orphan exit")
    if link.get("orphan_resolved"):
        raise ValueError("orphan already resolved")
    if entry_price <= 0:
        raise ValueError("entry_price must be positive")
    qty = float(f["qty"])
    notional = entry_price * qty
    exit_fee = float(f.get("fee_usd") or 0.0)
    pnl = (float(f["price"]) - entry_price) * qty - exit_fee
    epoch = await current_epoch()
    basis_ts = entry_ts or str(f.get("ts") or "")
    outcome = {
        "_id": f"rt:{fill_id}",
        "lane": f.get("lane"), "symbol": f.get("symbol"),
        "qty": round(qty, 10),
        "entry_avg_price": entry_price,
        "exit_price": f["price"],
        "entry_fill_ids": [],
        "exit_fill_id": fill_id,
        "entry_ts": entry_ts, "exit_ts": f.get("ts"),
        "holding_s": _holding_s(entry_ts, f.get("ts")) if entry_ts else None,
        "realized_pnl_usd": round(pnl, 6),
        "gross_return_pct": round(
            (float(f["price"]) - entry_price) / entry_price * 100.0, 6),
        "net_return_pct": round(pnl / notional * 100.0, 6),
        "fees_usd": round(exit_fee, 6),
        "fee_pct": None,
        "round_trip_cost_pct": None,
        "intent_id": intent_id or link.get("intent_id"),
        "brain": link.get("brain"), "stack": link.get("stack"),
        "position_id": position_id,
        "measured_cost_eligible": False,
        "resolution": "manual",
        "resolved_by": operator,
        "resolution_note": (note or "")[:300],
        "epoch_id": _epoch_for(basis_ts, epoch),
        "finalized_at": _now_iso(),
    }
    await db[OUTCOMES].update_one(
        {"_id": outcome["_id"]}, {"$set": outcome}, upsert=True)
    await db[LEDGER].update_one(
        {"_id": fill_id},
        {"$set": {"link.orphan_resolved": True,
                  "link.orphan_resolution": {
                      "entry_price": entry_price, "entry_ts": entry_ts,
                      "by": operator, "at": _now_iso(),
                      "note": (note or "")[:300]}}})
    logger.info("orphan exit resolved manually: %s @ basis %s by %s",
                fill_id, entry_price, operator)
    return outcome


async def worker_loop() -> None:
    logger.info("broker-fill reconciliation started (boot + 600s)")
    await asyncio.sleep(45)  # let adapters/credentials come up
    while True:
        try:
            await run_reconciliation(full=False)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("reconciliation loop error: %s", exc)
        await asyncio.sleep(600)
