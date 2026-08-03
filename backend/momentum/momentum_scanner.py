"""Momentum entry scanner (2026-08-01, operator package).

Builds MomentumSnapshots from live bars + quote enrichment for the
crypto BUY-allowlist universe and emits qualifying BUY intents into
the NORMAL pipeline as the 5th signal source (`stack="momentum"`).
Seat/risk/allowlist/entry-timing/broker gates stay authoritative.
Exits ride the existing exit monitor: positions whose origin stack is
`momentum` adopt +tp%/-sl% from broker cost basis (see monitor._adopt).
"""
from __future__ import annotations

import asyncio
import logging
import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from typing import Any, Optional

from momentum.momentum_position_controller import (
    AssetClass, EntryPolicy, MomentumSnapshot, valid_momentum_entry,
)

logger = logging.getLogger("risedual.momentum_scanner")

FLAG_ID = "momentum_scanner"
STATE_ID = "momentum_scanner_state"

DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "interval_sec": 60,
    "cooldown_min": 30,
    "max_emit_per_cycle": 2,
    "lanes": ["crypto", "equity"],
    "tp_pct": 5.0,
    "sl_pct": 3.0,
    "min_score": 0.60,
    "min_score_delta": 0.08,
}

EQUITY_SCAN_CAP = 30  # top live_universe movers per cycle


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def get_config() -> dict:
    from db import db  # noqa: WPS433
    doc = await db["runtime_flags"].find_one(
        {"_id": FLAG_ID}, {"_id": 0}, max_time_ms=3000) or {}
    return {**DEFAULTS, **doc}


async def get_momentum_exit_pcts() -> tuple[float, float]:
    """(tp_pct, sl_pct) for momentum-origin positions — the exit
    monitor anchors these on broker cost basis, never confirmation."""
    cfg = await get_config()
    return float(cfg["tp_pct"]), float(cfg["sl_pct"])


# ── pure tape math (deterministic, unit-tested) ──────────────────

def momentum_score(closes: list[float], vols: list[float]) -> Optional[float]:
    """0..1 momentum strength from the tape: 30-min drift + last-bar
    thrust + relative volume kicker. tanh-compressed so strong tapes
    don't saturate flat (the score DELTA is the transition signal)."""
    if len(closes) < 8 or closes[-7] <= 0 or closes[-2] <= 0:
        return None
    window_ret = (closes[-1] - closes[-7]) / closes[-7]
    bar_ret = (closes[-1] - closes[-2]) / closes[-2]
    rvol = relative_volume(vols)
    # Volume only confirms momentum on advancing bars — a red bar with
    # a volume spike is distribution, not ignition (ICNT 2026-08-03).
    # Kicker capped at 3x so rvol can never carry the score alone.
    vol_kick = 0.05 * (min(rvol, 3.0) - 1.0) if bar_ret > 0 else 0.0
    x = window_ret * 6.0 + bar_ret * 15.0 + vol_kick
    return round(0.5 + 0.5 * math.tanh(x), 4)


def confirmation_price(
    closes: list[float], vols: list[float],
    min_score: float = 0.60, lookback: int = 10,
) -> float:
    """Close of the bar where the score last crossed UP through
    `min_score` — the moment momentum was confirmed. The chase guard
    measures the run since THIS price, so stale momentum (confirmed
    many bars ago) is rejected as a chase."""
    n = len(closes)
    conf = closes[max(0, n - lookback)]
    for k in range(n - 1, max(8, n - lookback) - 1, -1):
        s = momentum_score(closes[:k], vols[:k])
        if s is None or s < min_score:
            return closes[k] if k < n else closes[-1]
        conf = closes[k - 1]
    return conf


def relative_volume(vols: list[float]) -> float:
    if len(vols) < 13:
        return 1.0
    base = sum(vols[-13:-1]) / 12.0
    return (vols[-1] / base) if base > 0 else 1.0


def build_snapshot(
    symbol: str, bars: list[dict], *,
    bid: float, ask: float, quote_age_ms: int,
    lane: str = "crypto", min_score: float = 0.60,
) -> Optional[MomentumSnapshot]:
    """Assemble the controller input from live bars + fresh quotes."""
    from shared.risk_sizer.entry_rearm import _ema, _session_vwap  # noqa: WPS433
    closes = [float(b.get("c") or 0) for b in bars]
    vols = [float(b.get("v") or 0) for b in bars]
    if len(closes) < 9 or any(c <= 0 for c in closes[-9:]):
        return None
    cur = momentum_score(closes, vols)
    prev = momentum_score(closes[:-1], vols[:-1])
    if cur is None or prev is None:
        return None
    mid = (bid + ask) / 2.0 if (bid > 0 and ask >= bid) else closes[-1]
    spread_bps = ((ask - bid) / mid * 10_000.0) if (bid > 0 and ask > bid) else 0.0
    ema9 = _ema(closes) or 0.0
    vwap = _session_vwap(bars) or 0.0
    r1 = closes[-1] / closes[-2] - 1.0
    r2 = closes[-2] / closes[-3] - 1.0 if closes[-3] > 0 else 0.0
    return MomentumSnapshot(
        symbol=symbol,
        asset_class=AssetClass.CRYPTO if lane == "crypto" else AssetClass.EQUITY,
        last_price=D(str(round(mid, 10))),
        previous_score=D(str(round(prev, 4))),
        current_score=D(str(round(cur, 4))),
        price_above_vwap=bool(vwap and mid >= vwap),
        price_above_ema9=bool(ema9 and mid >= ema9),
        acceleration_positive=r1 > r2 and r1 > 0,
        spread_bps=D(str(round(spread_bps, 2))),
        quote_age_ms=int(quote_age_ms),
        relative_volume=D(str(round(relative_volume(vols), 3))),
        confirmation_price=D(str(confirmation_price(
            closes, vols, min_score=min_score))),
        observed_at=_now(),
    )


# ── cycle ─────────────────────────────────────────────────────────

async def _already_engaged(db, symbol: str, cooldown_min: float) -> Optional[str]:
    """Skip reason if we hold the symbol or emitted recently."""
    plan = await db["shared_exit_plans"].find_one(
        {"symbol": symbol, "status": {"$in": ["active", "exiting"]}},
        {"_id": 1}, max_time_ms=3000)
    if plan:
        return "position_held"
    cut = (_now() - timedelta(minutes=cooldown_min)).isoformat()
    recent = await db["shared_intents"].find_one(
        {"stack": "momentum", "symbol": symbol, "ingest_ts": {"$gte": cut}},
        {"_id": 1}, max_time_ms=3000)
    if recent:
        return "cooldown"
    return None


async def _lane_symbols(lane: str) -> list[str]:
    """Crypto: the BUY allowlist (risk doctrine). Equity: live_universe
    movers — there is no equity allowlist; the scanner's own quality
    gates + seat/risk/entry-timing stand between signal and order."""
    if lane == "crypto":
        from shared.risk_sizer.buy_allowlist import get_allowlist  # noqa: WPS433
        allow = await get_allowlist()
        return sorted(allow.get("symbols") or [])
    from shared.universe.live_universe import read_all_universes  # noqa: WPS433
    docs = await read_all_universes()
    doc = (docs or {}).get("equity") or {}
    syms = {(s.get("canonical_symbol") or "").upper().strip()
            for s in (doc.get("symbols") or []) if s.get("tradable", True)}
    return sorted(x for x in syms if x)[:EQUITY_SCAN_CAP]


async def _lane_quote(lane: str, symbol: str) -> tuple[float, float, int]:
    """(bid, ask, quote_age_ms) — fail-soft zeros on missing quotes."""
    if lane == "crypto":
        from shared.market_data.crypto_snapshot_enrichment import (  # noqa: WPS433
            enrich_crypto_snapshot,
        )
        quote, diag = await enrich_crypto_snapshot({}, symbol=symbol)
        ladder = (diag or {}).get("ladder") or []
        age = int(ladder[0].get("age_ms") or 0) if ladder else 0
        return (float(quote.get("bid") or 0),
                float(quote.get("ask") or 0), age)
    from shared.snapshot_enrich.equity_doctrine import (  # noqa: WPS433
        enrich_equity_doctrine_snapshot,
    )
    snap = await enrich_equity_doctrine_snapshot(symbol, {})
    if (snap or {}).get("enrichment_status") != "live":
        return 0.0, 0.0, 0
    return float(snap.get("bid") or 0), float(snap.get("ask") or 0), 0


async def scan_once() -> dict:
    """One scanner pass over the enabled lanes."""
    from db import db  # noqa: WPS433
    from shared.risk_sizer.entry_timing import _load_bars  # noqa: WPS433
    from shared.market_hours import is_equity_rth  # noqa: WPS433

    cfg = await get_config()
    policy = EntryPolicy(
        min_score=D(str(cfg["min_score"])),
        min_score_delta=D(str(cfg["min_score_delta"])),
    )
    stats = {"evaluated": 0, "emitted": 0, "skipped": 0,
             "rejections": {}, "candidates": []}

    def _rej(reason: str) -> None:
        stats["rejections"][reason] = stats["rejections"].get(reason, 0) + 1

    for lane in [l for l in (cfg.get("lanes") or [])
                 if l in ("crypto", "equity")]:
        if lane == "equity" and not is_equity_rth():
            _rej("equity_market_closed")
            continue
        for sym in await _lane_symbols(lane):
            skip = await _already_engaged(db, sym, float(cfg["cooldown_min"]))
            if skip:
                stats["skipped"] += 1
                _rej(skip)
                continue
            bars = await _load_bars(sym)
            bid, ask, age_ms = await _lane_quote(lane, sym)
            if lane == "equity" and (bid <= 0 or ask <= 0):
                _rej("no_quote")  # equity fails closed on missing quotes
                continue
            snap = build_snapshot(
                sym, bars, bid=bid, ask=ask, quote_age_ms=age_ms,
                lane=lane, min_score=float(cfg["min_score"]))
            if snap is None:
                _rej("no_tape")
                continue
            stats["evaluated"] += 1
            decision = valid_momentum_entry(snap, policy)
            stats["candidates"].append({
                "symbol": sym, "lane": lane, "allowed": decision.allowed,
                "reason": decision.reason,
                "score": float(snap.current_score),
                "prev_score": float(snap.previous_score),
                "last_price": float(snap.last_price),
            })
            if not decision.allowed:
                _rej(decision.reason)
                continue
            if stats["emitted"] >= int(cfg["max_emit_per_cycle"]):
                _rej("cycle_emit_cap")
                continue
            try:
                from shared.intents import (  # noqa: WPS433
                    IntentIn, submit_intent_in_process,
                )
                body = IntentIn(
                    stack="momentum", action="BUY", symbol=sym, lane=lane,
                    confidence=round(min(0.95, float(snap.current_score)), 4),
                    rationale=(
                        f"momentum scanner [{lane}]: score "
                        f"{snap.previous_score}->{snap.current_score} · "
                        f"rvol {snap.relative_volume} · above vwap/ema9 · "
                        f"conf {snap.confirmation_price}"),
                    doctrine_snapshot={"bid": bid, "ask": ask},
                )
                res = await submit_intent_in_process(body)
                stats["emitted"] += 1
                logger.info("momentum_scanner: EMITTED %s %s intent=%s",
                            lane, sym, (res or {}).get("intent_id"))
            except Exception as exc:  # noqa: BLE001
                logger.warning("momentum_scanner: emit failed %s: %s",
                               sym, exc)
                _rej("emit_error")

    await db["runtime_flags"].update_one(
        {"_id": STATE_ID},
        {"$set": {"last_run": _now().isoformat(), **{
            k: stats[k] for k in ("evaluated", "emitted", "skipped",
                                  "rejections")},
            "candidates": stats["candidates"][:20]}},
        upsert=True)
    return stats


async def scanner_loop() -> None:
    logger.info("momentum scanner started (disabled until armed)")
    while True:
        try:
            cfg = await get_config()
            if cfg.get("enabled"):
                await scan_once()
            await asyncio.sleep(max(15, int(cfg.get("interval_sec", 60))))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("momentum_scanner loop error: %s", exc)
            await asyncio.sleep(60)
