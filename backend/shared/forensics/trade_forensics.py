"""Closed-trade forensics (2026-08-05 operator directive).

Every live round-trip since the cutoff, classified into the four loss
mechanisms — the report the operator asked for INSTEAD of another
architecture rebuild:
  bad_selection  — price went against the signal with no meaningful
                   favorable move first
  late_entry     — direction initially right (meaningful MFE) but
                   bought after the move; reversal took it out
  execution_cost — the loss is roughly the size of estimated costs
                   (spread + fees) — edge eaten by execution
  exit_policy    — the trade SAW take-profit-size gains and still
                   closed red — winners given back
Plus: winner / unknown (insufficient data).

Runs against whatever environment hosts it — in PREVIEW there are no
live outcomes, in PRODUCTION (after redeploy) it reads the real June+
receipts. MFE/MAE uses stored 5m bars when available, else best-effort
Kraken 1h/4h backfill (reaches ~120 days). Broker reconciliation:
operator posts the broker's monthly P&L figures; the report shows
internal vs broker deltas per month.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

logger = logging.getLogger("risedual.trade_forensics")

BROKER_ACTUALS_ID = "forensics_broker_actuals"
DEFAULT_SINCE = "2026-06-01T00:00:00+00:00"
EST_COST_PCT = 0.30  # spread+fees round-trip estimate, pct


def _f(v) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def classify_trade(t: dict) -> str:
    """Pure bucket verdict. t: realized_pnl_pct, mfe_pct, mae_pct,
    tp_pct, est_cost_pct."""
    pnl = _f(t.get("realized_pnl_pct"))
    if pnl is None:
        return "unknown"
    if pnl > 0:
        return "winner"
    mfe = _f(t.get("mfe_pct"))
    tp = _f(t.get("tp_pct")) or 5.0
    cost = _f(t.get("est_cost_pct")) or EST_COST_PCT
    if abs(pnl) <= cost * 1.5:
        return "execution_cost"
    if mfe is None:
        return "bad_selection"  # no tape → conservative default
    if mfe >= tp:
        return "exit_policy"
    if mfe >= max(0.3 * tp, abs(pnl) / 2.0):
        return "late_entry"
    return "bad_selection"


async def _bars_window(db, symbol: str, lane: str,
                       start_iso: str, end_iso: str) -> list[dict]:
    proj = {"_id": 0, "ts": 1, "h": 1, "l": 1, "c": 1}
    for tf in ("5m", "1h", "4h"):
        q = {"symbol": symbol, "tf": tf,
             "ts": {"$gte": start_iso, "$lte": end_iso}}
        rows = await db["shared_ohlcv_bars"].find(q, proj).sort(
            "ts", 1).max_time_ms(5000).to_list(600)
        if len(rows) >= 3:
            return rows
        if lane == "crypto" and tf in ("1h", "4h"):
            try:
                from shared.feeders.kraken_ohlc import (  # noqa: WPS433
                    _fetch_and_persist_one,
                )
                days = ((datetime.now(timezone.utc)
                         - datetime.fromisoformat(start_iso))
                        .total_seconds() / 86400.0) + 0.2
                if days <= (30 if tf == "1h" else 118):
                    await _fetch_and_persist_one(symbol, days, tf=tf)
                    rows = await db["shared_ohlcv_bars"].find(q, proj).sort(
                        "ts", 1).max_time_ms(5000).to_list(600)
                    if len(rows) >= 3:
                        return rows
            except Exception as exc:  # noqa: BLE001
                logger.warning("forensics bar fetch failed %s %s: %s",
                               symbol, tf, exc)
    return []


def excursions(entry: float, bars: list[dict]) -> tuple[Optional[float], Optional[float]]:
    """(mfe_pct, mae_pct) over the hold window."""
    if not bars or entry <= 0:
        return None, None
    hi = max((_f(b.get("h")) or 0) for b in bars)
    lows = [_f(b.get("l")) for b in bars if (_f(b.get("l")) or 0) > 0]
    lo = min(lows) if lows else None
    mfe = round((hi / entry - 1.0) * 100.0, 3) if hi > 0 else None
    mae = round((lo / entry - 1.0) * 100.0, 3) if lo else None
    return mfe, mae


async def entry_latency_report(db, n: int = 50) -> dict:
    """Signal→intent→submit→fill timeline for the last N live entries
    (2026-08-05 operator hypothesis: 'it buys after the gain').
    Per trade: delays at each hop, price at signal vs fill, the next
    30-min high after fill (bought near the top?) and the post-fill
    low (immediate adverse move). signal_ts exists on momentum
    intents going forward; pulse-brain intents use ingest_ts (pulse
    ticks every 15s, so signal→intent there is ≤15s by construction)."""
    exes = await db["executions"].find(
        {"action": {"$in": ["BUY", "SHORT"]}, "ok": True},
        {"_id": 0, "intent_id": 1, "ts": 1, "symbol": 1, "lane": 1,
         "brain": 1, "notional_usd": 1},
    ).sort("ts", -1).max_time_ms(8000).to_list(max(1, min(n, 200)))

    def _delta_s(a, b):
        try:
            return round((datetime.fromisoformat(b)
                          - datetime.fromisoformat(a)).total_seconds(), 1)
        except (TypeError, ValueError):
            return None

    rows, d_sig_int, d_int_sub, chases, top_shares = [], [], [], [], []
    for exe in exes:
        iid = exe.get("intent_id")
        intent = await db["shared_intents"].find_one(
            {"intent_id": iid},
            {"_id": 0, "ingest_ts": 1, "signal_bar_ts": 1,
             "signal_price": 1, "price_at_signal": 1, "snapshot": 1},
            max_time_ms=3000) if iid else None
        plan = await db["shared_exit_plans"].find_one(
            {"intent_id": iid}, {"_id": 0, "entry_price": 1},
            max_time_ms=3000) if iid else None
        submit_ts = exe.get("ts")
        sig_ts = (intent or {}).get("signal_bar_ts") \
            or (intent or {}).get("ingest_ts")
        ingest_ts = (intent or {}).get("ingest_ts")
        sig_to_intent = _delta_s(sig_ts, ingest_ts)
        intent_to_submit = _delta_s(ingest_ts, submit_ts)
        sig_price = _f((intent or {}).get("signal_price")) \
            or _f((intent or {}).get("price_at_signal")) \
            or _f(((intent or {}).get("snapshot") or {}).get("price"))
        fill = _f((plan or {}).get("entry_price"))
        chase_pct = (round((fill / sig_price - 1.0) * 100.0, 3)
                     if fill and sig_price else None)
        high_30 = low_30 = top_share = None
        if fill and submit_ts:
            try:
                end = (datetime.fromisoformat(submit_ts)
                       + timedelta(minutes=30)).isoformat()
                bars = await _bars_window(db, exe.get("symbol") or "",
                                          exe.get("lane") or "crypto",
                                          submit_ts, end)
                if bars:
                    high_30 = max((_f(b.get("h")) or 0) for b in bars)
                    lows = [_f(b.get("l")) for b in bars
                            if (_f(b.get("l")) or 0) > 0]
                    low_30 = min(lows) if lows else None
                    if high_30 and high_30 > 0:
                        top_share = round(fill / high_30, 4)
            except Exception:  # noqa: BLE001
                pass
        rows.append({
            "symbol": exe.get("symbol"), "lane": exe.get("lane"),
            "brain": exe.get("brain"),
            "signal_ts": sig_ts, "intent_ts": ingest_ts,
            "submit_ts": submit_ts,
            "signal_to_intent_s": sig_to_intent,
            "intent_to_submit_s": intent_to_submit,
            "price_at_signal": sig_price, "fill_price": fill,
            "chase_pct": chase_pct,
            "high_next_30m": high_30, "low_next_30m": low_30,
            "fill_vs_30m_high": top_share,
        })
        if sig_to_intent is not None:
            d_sig_int.append(sig_to_intent)
        if intent_to_submit is not None:
            d_int_sub.append(intent_to_submit)
        if chase_pct is not None:
            chases.append(chase_pct)
        if top_share is not None:
            top_shares.append(top_share)

    def _med(vals):
        s = sorted(vals)
        return round(s[len(s) // 2], 2) if s else None

    return {
        "n": len(rows), "trades": rows,
        "aggregates": {
            "median_signal_to_intent_s": _med(d_sig_int),
            "median_intent_to_submit_s": _med(d_int_sub),
            "median_chase_pct": _med(chases),
            "median_fill_vs_30m_high": _med(top_shares),
            "pct_bought_within_1pct_of_30m_top": (
                round(100.0 * sum(1 for t in top_shares if t >= 0.99)
                      / len(top_shares), 1) if top_shares else None),
        },
        "cadences": {
            "mc_pulse_tick_s": 15, "momentum_scanner_s": 60,
            "universe_refresh_min": 15,
            "note": ("polling is fast; the historical 15-30min lag "
                     "matches universe-membership discovery delay — "
                     "ignition watch (2026-08-04) closes that gap; "
                     "this report verifies it on forward trades"),
        },
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "note": ("no live entries recorded in this environment — run "
                 "in PRODUCTION for the real timeline"
                 if not rows else None),
    }


async def closed_trade_report(db, since_iso: str = DEFAULT_SINCE,
                              with_bars: bool = True) -> dict:
    outcomes = await db["shared_exit_outcomes"].find(
        {"closed_at": {"$gte": since_iso}}, {"_id": 0},
    ).sort("closed_at", 1).max_time_ms(10000).to_list(500)

    trades, buckets = [], {}
    by = {"lane": {}, "brain": {}, "exit_reason": {}, "month": {},
          "entry_source": {}}

    def _agg(group: dict, key: str, pnl: float) -> None:
        g = group.setdefault(key or "unknown", {"n": 0, "pnl_usd": 0.0,
                                                "wins": 0})
        g["n"] += 1
        g["pnl_usd"] = round(g["pnl_usd"] + pnl, 2)
        g["wins"] += 1 if pnl > 0 else 0

    for oc in outcomes:
        sym = oc.get("symbol") or ""
        entry = _f(oc.get("entry_price")) or 0.0
        opened = oc.get("opened_at") or oc.get("created_at")
        closed = oc.get("closed_at")
        exe = await db["executions"].find_one(
            {"intent_id": oc.get("intent_id")},
            {"_id": 0, "brain": 1, "seat_holder": 1, "seats": 1},
            max_time_ms=3000) if oc.get("intent_id") else None
        mfe = mae = None
        if with_bars and entry > 0 and opened and closed:
            bars = await _bars_window(db, sym, oc.get("lane") or "crypto",
                                      opened, closed)
            mfe, mae = excursions(entry, bars)
        row = {
            "symbol": sym, "lane": oc.get("lane"),
            "entry_source": oc.get("entry_source"),
            "brain": (exe or {}).get("brain") or (exe or {}).get("seat_holder"),
            "entry_price": entry or None,
            "exit_price": _f(oc.get("exit_price")),
            "realized_pnl_pct": _f(oc.get("realized_pnl_pct")),
            "realized_pnl_usd": _f(oc.get("realized_pnl_usd")),
            "exit_reason": oc.get("exit_reason") or oc.get("outcome"),
            "opened_at": opened, "closed_at": closed,
            "mfe_pct": mfe, "mae_pct": mae,
            "tp_pct": _f(oc.get("tp_pct")) or 5.0,
            "est_cost_pct": EST_COST_PCT,
        }
        row["bucket"] = classify_trade(row)
        trades.append(row)
        pnl = row["realized_pnl_usd"] or 0.0
        buckets.setdefault(row["bucket"], {"n": 0, "pnl_usd": 0.0})
        buckets[row["bucket"]]["n"] += 1
        buckets[row["bucket"]]["pnl_usd"] = round(
            buckets[row["bucket"]]["pnl_usd"] + pnl, 2)
        _agg(by["lane"], row["lane"], pnl)
        _agg(by["brain"], row["brain"], pnl)
        _agg(by["exit_reason"], str(row["exit_reason"]), pnl)
        _agg(by["entry_source"], str(row["entry_source"]), pnl)
        _agg(by["month"], str(closed or "")[:7], pnl)

    loss_buckets = {k: v for k, v in buckets.items()
                    if k not in ("winner", "unknown")}
    dominant = max(loss_buckets, key=lambda k: -loss_buckets[k]["pnl_usd"]) \
        if loss_buckets else None
    actuals = await db["runtime_flags"].find_one(
        {"_id": BROKER_ACTUALS_ID}, {"_id": 0}, max_time_ms=3000) or {}
    recon = []
    for month, g in sorted(by["month"].items()):
        broker = _f((actuals.get("months") or {}).get(month))
        recon.append({
            "month": month, "internal_pnl_usd": g["pnl_usd"],
            "broker_pnl_usd": broker,
            "delta_usd": (round(g["pnl_usd"] - broker, 2)
                          if broker is not None else None),
        })
    return {
        "since": since_iso, "n_trades": len(trades),
        "total_pnl_usd": round(sum(t["realized_pnl_usd"] or 0
                                   for t in trades), 2),
        "buckets": buckets, "dominant_loss_mechanism": dominant,
        "by": by, "reconciliation": recon,
        "broker_actuals": actuals.get("months") or {},
        "trades": trades[-100:],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "note": ("no closed trades in this environment's database since "
                 "cutoff — run in PRODUCTION for the live report"
                 if not trades else None),
    }
