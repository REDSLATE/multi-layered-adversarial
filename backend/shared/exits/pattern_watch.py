"""Sell-Point Watcher (2026-08-04, v3.5 adoption plan item 5).

Bearish-structure detection on HELD tickers (symbols with active exit
plans): double_top · head_shoulders · rising_wedge, from fractal
pivots (k=2) on 5m bars. OBSERVE-FIRST doctrine: ships in `observe`
mode — detections write receipts to `sell_point_events` only. In
`act` mode the per-pattern action applies:
  tighten — raise the plan's stop to (close − stop_buffer_atr×ATR),
            NEVER lower, never at/above price
  exit    — trigger the exit monitor's manual close ladder
Knobs in `runtime_flags._id=sell_point_watch`. Tape Quality Gate
protects every evaluation. Events dedupe per (plan, pattern) on a
cooldown so one forming pattern can't spam actions.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

logger = logging.getLogger("risedual.sell_point_watch")

FLAG_ID = "sell_point_watch"
EVENTS = "sell_point_events"
DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "mode": "observe",
    "interval_sec": 120,
    "cooldown_min": 60,
    "stop_buffer_atr": 0.5,
    "actions": {"double_top": "tighten", "head_shoulders": "tighten",
                "rising_wedge": "tighten"},
}
PATTERNS = ("double_top", "head_shoulders", "rising_wedge")


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def get_config() -> dict:
    from db import db  # noqa: WPS433
    doc = await db["runtime_flags"].find_one(
        {"_id": FLAG_ID}, {"_id": 0}, max_time_ms=3000) or {}
    merged = {**DEFAULTS, **doc}
    merged["actions"] = {**DEFAULTS["actions"], **(doc.get("actions") or {})}
    return merged


# ── pure structure math ─────────────────────────────────────────────

def pivot_indices(vals: list[float], k: int = 2, high: bool = True) -> list[int]:
    out = []
    for i in range(k, len(vals) - k):
        window = vals[i - k:i] + vals[i + 1:i + k + 1]
        if high and all(vals[i] > w for w in window):
            out.append(i)
        elif not high and all(vals[i] < w for w in window):
            out.append(i)
    return out


def atr(bars: list[dict], n: int = 14) -> float:
    trs = []
    for i in range(max(1, len(bars) - n), len(bars)):
        h = float(bars[i].get("h") or 0)
        l = float(bars[i].get("l") or 0)
        pc = float(bars[i - 1].get("c") or 0)
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs) / len(trs) if trs else 0.0


def _slope(points: list[tuple[int, float]]) -> Optional[float]:
    n = len(points)
    if n < 2:
        return None
    mx = sum(p[0] for p in points) / n
    my = sum(p[1] for p in points) / n
    den = sum((p[0] - mx) ** 2 for p in points)
    if den == 0:
        return None
    return sum((p[0] - mx) * (p[1] - my) for p in points) / den


def detect_double_top(bars: list[dict]) -> Optional[dict]:
    if len(bars) < 25:
        return None
    highs = [float(b.get("h") or 0) for b in bars]
    lows = [float(b.get("l") or 0) for b in bars]
    close = float(bars[-1].get("c") or 0)
    a = atr(bars)
    piv = pivot_indices(highs, high=True)
    if len(piv) < 2 or a <= 0 or close <= 0:
        return None
    i1, i2 = piv[-2], piv[-1]
    if i2 < len(bars) - 15 or i2 - i1 < 3:
        return None  # second peak must be recent, peaks separated
    if abs(highs[i1] - highs[i2]) > 0.5 * a:
        return None
    neckline = min(lows[i1:i2 + 1])
    if close >= neckline:
        return None
    return {"pattern": "double_top", "atr": round(a, 8), "close": close,
            "neckline": round(neckline, 8),
            "peaks": [{"i": i1, "price": highs[i1]},
                      {"i": i2, "price": highs[i2]}]}


def detect_head_shoulders(bars: list[dict]) -> Optional[dict]:
    if len(bars) < 30:
        return None
    highs = [float(b.get("h") or 0) for b in bars]
    lows = [float(b.get("l") or 0) for b in bars]
    close = float(bars[-1].get("c") or 0)
    a = atr(bars)
    piv = pivot_indices(highs, high=True)
    if len(piv) < 3 or a <= 0 or close <= 0:
        return None
    i1, i2, i3 = piv[-3], piv[-2], piv[-1]
    if i3 < len(bars) - 15:
        return None
    head, ls, rs = highs[i2], highs[i1], highs[i3]
    if not (head > ls and head > rs):
        return None
    if abs(ls - rs) > 1.0 * a:
        return None  # shoulders roughly level
    neckline = min(min(lows[i1:i2 + 1]), min(lows[i2:i3 + 1]))
    if close >= neckline:
        return None
    return {"pattern": "head_shoulders", "atr": round(a, 8),
            "close": close, "neckline": round(neckline, 8),
            "peaks": [{"i": i1, "price": ls}, {"i": i2, "price": head},
                      {"i": i3, "price": rs}]}


def detect_rising_wedge(bars: list[dict], window: int = 30) -> Optional[dict]:
    if len(bars) < window:
        return None
    seg = bars[-window:]
    highs = [float(b.get("h") or 0) for b in seg]
    lows = [float(b.get("l") or 0) for b in seg]
    vols = [float(b.get("v") or 0) for b in seg]
    close = float(seg[-1].get("c") or 0)
    ph = pivot_indices(highs, high=True)
    pl = pivot_indices(lows, high=False)
    if len(ph) < 2 or len(pl) < 2 or close <= 0:
        return None
    sh = _slope([(i, highs[i]) for i in ph])
    sl = _slope([(i, lows[i]) for i in pl])
    if sh is None or sl is None or sh <= 0 or sl <= 0 or sh >= sl:
        return None  # need rising + converging (lows rising faster)
    third = max(1, window // 3)
    v_early = sum(vols[:third]) / third
    v_late = sum(vols[-third:]) / third
    if v_early <= 0 or v_late > 0.8 * v_early:
        return None  # volume must fade into the wedge
    # lower trendline value at the last bar
    n = len(pl)
    mx = sum(pl) / n
    my = sum(lows[i] for i in pl) / n
    trendline = my + sl * ((window - 1) - mx)
    if close >= trendline:
        return None  # not broken yet
    return {"pattern": "rising_wedge", "atr": round(atr(seg), 8),
            "close": close, "neckline": round(trendline, 8),
            "slope_highs": round(sh, 8), "slope_lows": round(sl, 8),
            "volume_fade": round(v_late / v_early, 3)}


def detect_all(bars: list[dict]) -> list[dict]:
    hits = []
    for fn in (detect_double_top, detect_head_shoulders,
               detect_rising_wedge):
        try:
            hit = fn(bars)
        except Exception as exc:  # noqa: BLE001
            logger.warning("sell_point detector %s errored: %s",
                           fn.__name__, exc)
            hit = None
        if hit:
            hits.append(hit)
    return hits


def tighten_stop(plan: dict, hit: dict, buffer_atr: float) -> Optional[float]:
    """New stop = close − buffer×ATR. NEVER lower than the current
    stop, never at/above price. None = no change."""
    close, a = float(hit["close"]), float(hit["atr"])
    if close <= 0 or a <= 0:
        return None
    new_stop = round(close - buffer_atr * a, 8)
    cur = float(plan.get("stop_price") or 0)
    if new_stop <= cur or new_stop >= close:
        return None
    return new_stop


# ── cycle ───────────────────────────────────────────────────────────

async def _bars_5m(db, symbol: str, limit: int = 60) -> list[dict]:
    rows = await db["shared_ohlcv_bars"].find(
        {"symbol": symbol, "tf": "5m"},
        {"_id": 0, "ts": 1, "o": 1, "h": 1, "l": 1, "c": 1, "v": 1},
    ).sort("ts", -1).max_time_ms(4000).to_list(limit)
    return list(reversed(rows))


async def _on_cooldown(db, plan_id: str, pattern: str,
                       cooldown_min: float) -> bool:
    cut = (_now() - timedelta(minutes=cooldown_min)).isoformat()
    return bool(await db[EVENTS].find_one(
        {"plan_id": plan_id, "pattern": pattern,
         "created_at": {"$gte": cut}}, {"_id": 1}, max_time_ms=3000))


async def _apply_action(plan: dict, hit: dict, action: str,
                        cfg: dict) -> dict:
    if action == "exit":
        from shared.exits import monitor  # noqa: WPS433
        res = await monitor.close_now(plan["plan_id"])
        return {"action": "exit", "applied": bool(res.get("ok")),
                "detail": res.get("error")}
    new_stop = tighten_stop(plan, hit, float(cfg["stop_buffer_atr"]))
    if new_stop is None:
        return {"action": "tighten", "applied": False,
                "detail": "stop_not_raised"}
    from shared.hotpath import exit_plans as plan_store  # noqa: WPS433
    plan_store.update(plan["plan_id"], {
        "stop_price": new_stop, "levels_source": "sell_point_watch",
        "sell_point_pattern": hit["pattern"]})
    return {"action": "tighten", "applied": True,
            "prev_stop": float(plan.get("stop_price") or 0),
            "new_stop": new_stop}


async def run_once() -> dict:
    from db import db  # noqa: WPS433
    from shared.hotpath import exit_plans as plan_store  # noqa: WPS433
    from shared.market_data.tape_quality import assess_with_config  # noqa: WPS433
    cfg = await get_config()
    stats = {"plans": 0, "evaluated": 0, "detected": 0, "applied": 0,
             "skipped": {}}

    def _skip(reason: str) -> None:
        stats["skipped"][reason] = stats["skipped"].get(reason, 0) + 1

    if not cfg.get("enabled", True):
        return {**stats, "disabled": True}
    plans = [p for p in plan_store.load_live() if p.get("status") == "active"]
    stats["plans"] = len(plans)
    for plan in plans:
        sym = plan.get("symbol") or ""
        bars = await _bars_5m(db, sym)
        try:
            tq = await assess_with_config(bars)
        except Exception:  # noqa: BLE001
            tq = {"ok": True}
        if not tq.get("ok"):
            _skip(tq.get("reason") or "bad_tape")
            continue
        stats["evaluated"] += 1
        for hit in detect_all(bars):
            pattern = hit["pattern"]
            action = str((cfg.get("actions") or {}).get(pattern)
                         or "tighten").lower()
            if action == "off":
                _skip(f"{pattern}_off")
                continue
            if await _on_cooldown(db, plan["plan_id"], pattern,
                                  float(cfg["cooldown_min"])):
                _skip("cooldown")
                continue
            stats["detected"] += 1
            outcome = {"action": action, "applied": False,
                       "detail": "observe_mode"}
            if str(cfg.get("mode") or "observe").lower() == "act":
                outcome = await _apply_action(plan, hit, action, cfg)
            if outcome.get("applied"):
                stats["applied"] += 1
            await db[EVENTS].insert_one({
                "_id": f"spw-{uuid.uuid4().hex[:16]}",
                "plan_id": plan["plan_id"], "symbol": sym,
                "lane": plan.get("lane"), "mode": cfg.get("mode"),
                **hit, **outcome, "created_at": _now().isoformat(),
            })
            logger.info(
                "sell_point_watch: %s on %s → %s (%s, applied=%s)",
                pattern, sym, action, cfg.get("mode"),
                outcome.get("applied"))
    return stats


async def worker_loop() -> None:
    logger.info("sell-point watcher started (mode per runtime_flags)")
    while True:
        try:
            cfg = await get_config()
            await asyncio.sleep(max(30, int(cfg.get("interval_sec", 120))))
            await run_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("sell_point_watch loop error: %s", exc)
            await asyncio.sleep(120)
