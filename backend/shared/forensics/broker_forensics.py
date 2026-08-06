"""Broker-sourced forensics (2026-08 directive).

The internal round-trip chain was empty from day one — 47 filled
entries, zero exit receipts, zero outcomes — because exit lanes
shipped disabled. The broker still holds the truth: every fill.
This module pulls Webull's filled-order history, reconstructs FIFO
round trips per symbol, and reports exactly where the money went.
Immune to database wipes; needs only broker credentials.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger("risedual.broker_forensics")


def _parse_ts(v) -> Optional[datetime]:
    if v is None:
        return None
    if isinstance(v, str) and v.strip().isdigit():
        v = float(v)
    if isinstance(v, (int, float)):
        ms = float(v)
        if ms > 1e12:
            ms /= 1000.0
        try:
            return datetime.fromtimestamp(ms, tz=timezone.utc)
        except (ValueError, OSError):
            return None
    s = str(v).strip().replace("Z", "+00:00")
    for fmt in (None, "%Y-%m-%d %H:%M:%S", "%m/%d/%Y %H:%M:%S"):
        try:
            dt = (datetime.fromisoformat(s) if fmt is None
                  else datetime.strptime(s, fmt))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def build_round_trips(fills: list[dict], max_hold_h: float = 24.0) -> dict:
    """FIFO-match BUY lots against SELL fills per symbol."""
    by_symbol: dict[str, list[dict]] = {}
    for f in fills:
        if f.get("symbol"):
            by_symbol.setdefault(f["symbol"], []).append(f)

    trips: list[dict] = []
    open_lots: list[dict] = []
    for symbol, rows in by_symbol.items():
        rows.sort(key=lambda r: _parse_ts(r.get("filled_at"))
                  or datetime.min.replace(tzinfo=timezone.utc))
        lots: list[dict] = []  # open BUY lots (FIFO queue)
        for f in rows:
            side = (f.get("side") or "").upper()
            qty, price = float(f["qty"]), float(f["price"])
            fee = float(f.get("fee") or 0)
            ts = _parse_ts(f.get("filled_at"))
            if side == "BUY":
                lots.append({"qty": qty, "price": price, "fee": fee, "ts": ts})
                continue
            if side != "SELL":
                continue
            remaining, sell_fee_left = qty, fee
            while remaining > 1e-9 and lots:
                lot = lots[0]
                matched = min(remaining, lot["qty"])
                frac = matched / lot["qty"] if lot["qty"] else 0
                entry_fee = lot["fee"] * frac
                sell_fee = sell_fee_left * (matched / qty) if qty else 0
                pnl = (price - lot["price"]) * matched - entry_fee - sell_fee
                hold_h = None
                if ts and lot["ts"]:
                    hold_h = round((ts - lot["ts"]).total_seconds() / 3600, 2)
                pnl_pct = (round((price / lot["price"] - 1) * 100, 3)
                           if lot["price"] else None)
                trips.append({
                    "symbol": symbol, "qty": round(matched, 6),
                    "entry_price": lot["price"], "exit_price": price,
                    "entry_at": lot["ts"].isoformat() if lot["ts"] else None,
                    "exit_at": ts.isoformat() if ts else None,
                    "hold_h": hold_h,
                    "pnl_usd": round(pnl, 4), "pnl_pct": pnl_pct,
                    "fees_usd": round(entry_fee + sell_fee, 4),
                    "verdict": _verdict(pnl, pnl_pct, hold_h,
                                        entry_fee + sell_fee, max_hold_h),
                })
                lot["qty"] -= matched
                lot["fee"] -= entry_fee
                remaining -= matched
                sell_fee_left -= sell_fee
                if lot["qty"] <= 1e-9:
                    lots.pop(0)
        for lot in lots:
            open_lots.append({
                "symbol": symbol, "qty": round(lot["qty"], 6),
                "entry_price": lot["price"],
                "entry_at": lot["ts"].isoformat() if lot["ts"] else None,
            })
    trips.sort(key=lambda t: t.get("exit_at") or "", reverse=True)
    return {"round_trips": trips, "open_lots": open_lots}


def _verdict(pnl: float, pnl_pct: Optional[float], hold_h: Optional[float],
             fees: float, max_hold_h: float) -> str:
    if pnl > 0:
        return "winner"
    if abs(pnl) <= max(fees * 1.5, 0.05):
        return "execution_cost"
    if hold_h is not None and hold_h > max_hold_h:
        return "unmanaged_hold"  # held past max-hold with NO exit plan
    return "bad_selection"


def summarize(trips: list[dict], open_lots: list[dict],
              fills: list[dict]) -> dict:
    wins = [t for t in trips if t["pnl_usd"] > 0]
    losses = [t for t in trips if t["pnl_usd"] <= 0]
    monthly: dict[str, float] = {}
    for t in trips:
        m = (t.get("exit_at") or "")[:7]
        if m:
            monthly[m] = round(monthly.get(m, 0) + t["pnl_usd"], 2)
    buckets: dict[str, dict] = {}
    for t in trips:
        b = buckets.setdefault(t["verdict"], {"n": 0, "pnl_usd": 0.0})
        b["n"] += 1
        b["pnl_usd"] = round(b["pnl_usd"] + t["pnl_usd"], 2)
    loss_buckets = {k: v for k, v in buckets.items() if k != "winner"}
    dominant = (max(loss_buckets, key=lambda k: abs(loss_buckets[k]["pnl_usd"]))
                if loss_buckets else None)
    holds = sorted(t["hold_h"] for t in trips if t.get("hold_h") is not None)
    return {
        "n_fills": len(fills),
        "n_round_trips": len(trips),
        "n_open_lots": len(open_lots),
        "wins": len(wins), "losses": len(losses),
        "win_rate_pct": round(len(wins) / len(trips) * 100, 1) if trips else None,
        "realized_pnl_usd": round(sum(t["pnl_usd"] for t in trips), 2),
        "total_fees_usd": round(sum(t["fees_usd"] for t in trips), 2),
        "avg_win_usd": round(sum(t["pnl_usd"] for t in wins) / len(wins), 2)
        if wins else None,
        "avg_loss_usd": round(sum(t["pnl_usd"] for t in losses) / len(losses), 2)
        if losses else None,
        "median_hold_h": holds[len(holds) // 2] if holds else None,
        "max_hold_h": holds[-1] if holds else None,
        "monthly_pnl": dict(sorted(monthly.items())),
        "buckets": buckets,
        "dominant_loss_mechanism": dominant,
    }


async def broker_report(db, start: Optional[str] = None,
                        end: Optional[str] = None) -> dict[str, Any]:
    """Full Webull-sourced forensic report. start/end: yyyy-MM-dd."""
    from shared.broker.webull import get_webull_adapter  # noqa: WPS433
    now = datetime.now(timezone.utc)
    adapter = await get_webull_adapter()
    if adapter is None:
        return {"ok": False, "broker": "webull",
                "error": "Webull adapter unavailable — check credentials"}
    fills = await adapter.list_history(start=start, end=end)
    try:
        from shared.exits.policy import get_policy  # noqa: WPS433
        max_hold_h = float((await get_policy())["equity"]["max_hold_h"])
    except Exception:  # noqa: BLE001
        max_hold_h = 24.0
    built = build_round_trips(fills, max_hold_h=max_hold_h)
    report = {
        "ok": True, "broker": "webull",
        "start": start, "end": end,
        "generated_at": now.isoformat(),
        **summarize(built["round_trips"], built["open_lots"], fills),
        "round_trips": built["round_trips"][:200],
        "open_lots": built["open_lots"][:100],
    }
    if not fills:
        report["note"] = ("no filled orders returned by Webull for this "
                          "window — widen start/end (format yyyy-MM-dd; "
                          "Webull defaults to last 7 days when empty)")
    return report
