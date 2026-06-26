"""Brain Input Health — operator visibility for instrument quality.

Operator concern (2026-02-23): "I'm fine with intents but I can't
know which one is good to go if the instruments are failing to
report accurate information."

This endpoint answers that concern at two levels:

  1. Per brain — for each of the 4 native brains, what fraction of
     the equity universe carries a FRESH and COMPLETE indicator
     snapshot that this brain can actually evaluate? Pairs with
     last-emit / 24h-emit counts so a silent brain stands out.

  2. Per symbol — for each universe symbol, what's the snapshot
     age, bar count, and which of the 4 brains' required-fields
     contracts it currently satisfies. So when Barracuda emits a
     BUY on AAPL, the operator can confirm AAPL's snapshot was
     fresh + complete and not a stale-data ghost signal.

Required-field contracts match each brain's `strategy.evaluate`
`missing` check — single source of truth across both modules so
this view stays honest if the strategy adds a new dependency.

NO mutations. NO execution-path side-effects. Pure diagnostic.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends

from auth import get_current_user
from db import db


router = APIRouter(tags=["admin"])


# ─────────────────── brain required-field contracts ───────────────────
# These mirror each `strategy.evaluate`'s `missing` check exactly. If
# you change one, change the other (or add a regression test).


def _check_barracuda(ind: dict) -> list[str]:
    missing: list[str] = []
    if ind.get("last_close") is None:
        missing.append("last_close")
    if ind.get("rsi14") is None:
        missing.append("rsi14")
    bb = ind.get("bbands") or {}
    if bb.get("position") is None or bb.get("mid") is None:
        missing.append("bbands")
    sma = ind.get("sma") or {}
    if sma.get("20") is None or sma.get("50") is None:
        missing.append("sma")
    atr = ind.get("atr14")
    if atr is None or (isinstance(atr, (int, float)) and atr <= 0):
        missing.append("atr14")
    return missing


def _check_gto(ind: dict) -> list[str]:
    missing: list[str] = []
    if ind.get("last_close") is None:
        missing.append("last_close")
    if ind.get("rsi14") is None:
        missing.append("rsi14")
    macd = ind.get("macd") or {}
    if macd.get("hist") is None:
        missing.append("macd_hist")
    ema = ind.get("ema") or {}
    if ema.get("12") is None or ema.get("26") is None:
        missing.append("ema")
    sma = ind.get("sma") or {}
    if sma.get("20") is None:
        missing.append("sma20")
    atr = ind.get("atr14")
    if atr is None or (isinstance(atr, (int, float)) and atr <= 0):
        missing.append("atr14")
    return missing


def _check_camino(ind: dict) -> list[str]:
    missing: list[str] = []
    if ind.get("last_close") is None:
        missing.append("last_close")
    if ind.get("rsi14") is None:
        missing.append("rsi14")
    sma = ind.get("sma") or {}
    if sma.get("20") is None or sma.get("50") is None:
        missing.append("sma")
    ema = ind.get("ema") or {}
    if ema.get("12") is None:
        missing.append("ema12")
    atr = ind.get("atr14")
    if atr is None or (isinstance(atr, (int, float)) and atr <= 0):
        missing.append("atr14")
    return missing


def _check_hellcat(ind: dict) -> list[str]:
    missing: list[str] = []
    if ind.get("last_close") is None:
        missing.append("last_close")
    if ind.get("rsi14") is None:
        missing.append("rsi14")
    bb = ind.get("bbands") or {}
    if bb.get("position") is None or bb.get("upper") is None or bb.get("lower") is None:
        missing.append("bbands")
    sma = ind.get("sma") or {}
    if sma.get("20") is None:
        missing.append("sma20")
    atr = ind.get("atr14")
    if atr is None or (isinstance(atr, (int, float)) and atr <= 0):
        missing.append("atr14")
    return missing


BRAIN_CHECKERS = {
    "barracuda": ("mean_reversion", _check_barracuda),
    "gto":       ("momentum",       _check_gto),
    "camino":    ("trend",          _check_camino),
    "hellcat":   ("breakout",       _check_hellcat),
}


# ─────────────────────────── helpers ───────────────────────────


# Snapshots older than this are flagged stale (during market hours
# 5-min snapshots refresh every ~60s; we accept up to 10 min before
# flagging — survives a single missed cycle).
STALE_THRESHOLD_SEC = 10 * 60
# Bars-seen below this and the indicator math is statistically thin
# (atr14, sma50 both want ≥60 bars to be meaningful).
MIN_RELIABLE_BARS = 60


def _parse_iso(ts: Any) -> Optional[datetime]:
    if not isinstance(ts, str):
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        return None


async def _load_universe() -> list[str]:
    cursor = db["patterns_universe"].find(
        {"$or": [{"lane": "equity"}, {"lane": {"$exists": False}}]},
        {"_id": 0, "symbol": 1},
    )
    symbols: set[str] = set()
    async for row in cursor:
        s = (row.get("symbol") or "").strip().upper()
        if s and s.isalnum():
            symbols.add(s)
    return sorted(symbols)


async def _latest_snapshot(symbol: str) -> Optional[dict]:
    return await db["shared_indicator_snapshots"].find_one(
        {"symbol": symbol},
        {"_id": 0},
        sort=[("computed_at", -1)],
    )


async def _emit_stats_for_brain(brain_id: str, now: datetime) -> dict:
    cutoff_24h = (now - timedelta(hours=24)).isoformat()
    cutoff_7d = (now - timedelta(days=7)).isoformat()

    last = await db["shared_intents"].find_one(
        {"stack_canonical": brain_id},
        {"_id": 0, "created_at": 1, "action": 1, "symbol": 1, "rationale": 1},
        sort=[("created_at", -1)],
    )
    last_dir = await db["shared_intents"].find_one(
        {
            "stack_canonical": brain_id,
            "action": {"$in": ["BUY", "SELL", "SHORT", "COVER"]},
        },
        {"_id": 0, "created_at": 1, "action": 1, "symbol": 1},
        sort=[("created_at", -1)],
    )

    emits_24h = await db["shared_intents"].count_documents({
        "stack_canonical": brain_id,
        "created_at": {"$gte": cutoff_24h},
    })
    emits_7d = await db["shared_intents"].count_documents({
        "stack_canonical": brain_id,
        "created_at": {"$gte": cutoff_7d},
    })

    # Action breakdown last 7d
    pipeline = [
        {"$match": {
            "stack_canonical": brain_id,
            "created_at": {"$gte": cutoff_7d},
        }},
        {"$group": {"_id": "$action", "n": {"$sum": 1}}},
    ]
    actions_7d = {}
    async for row in db["shared_intents"].aggregate(pipeline):
        actions_7d[row["_id"] or "UNKNOWN"] = row["n"]

    def _age(iso: Optional[str]) -> Optional[float]:
        if not iso:
            return None
        dt = _parse_iso(iso)
        if dt is None:
            return None
        return (now - dt).total_seconds()

    return {
        "brain_id": brain_id,
        "doctrine": BRAIN_CHECKERS[brain_id][0],
        "last_emit": last,
        "last_emit_age_sec": _age((last or {}).get("created_at")),
        "last_directional_emit": last_dir,
        "last_directional_age_sec": _age((last_dir or {}).get("created_at")),
        "emits_24h": emits_24h,
        "emits_7d": emits_7d,
        "actions_7d": actions_7d,
        "directional_pct_7d": (
            round(100.0 * sum(
                actions_7d.get(a, 0) for a in ("BUY", "SELL", "SHORT", "COVER")
            ) / max(1, emits_7d), 1)
            if emits_7d > 0 else None
        ),
    }


@router.get("/admin/brain-input-health")
async def admin_brain_input_health(
    user: dict = Depends(get_current_user),  # noqa: B008, ARG001
):
    """Brain Input Health — see module docstring for doctrine.

    Response shape:
        {
          ok: true,
          as_of: ISO,
          stale_threshold_sec: 600,
          min_reliable_bars: 60,
          brains: [
            { brain_id, doctrine, last_emit, last_emit_age_sec,
              last_directional_emit, last_directional_age_sec,
              emits_24h, emits_7d, actions_7d, directional_pct_7d,
              evaluable_count, evaluable_pct,
            }, …
          ],
          universe: [
            { symbol, snapshot_age_sec, bars_seen, bars_thin,
              last_close, rsi14, missing_for, stale, has_snapshot
            }, …
          ],
          summary: {
            universe_size, fresh_count, stale_count,
            missing_snapshot_count, thin_bars_count,
            evaluable_all_brains_count, evaluable_by_brain
          }
        }
    """
    now = datetime.now(timezone.utc)
    universe = await _load_universe()

    # ── per-symbol roll-up ──
    rows: list[dict] = []
    evaluable_by_brain = {b: 0 for b in BRAIN_CHECKERS}
    fresh_count = 0
    stale_count = 0
    missing_snapshot_count = 0
    thin_bars_count = 0

    for symbol in universe:
        snap = await _latest_snapshot(symbol)
        if not snap:
            rows.append({
                "symbol": symbol,
                "has_snapshot": False,
                "snapshot_age_sec": None,
                "bars_seen": None,
                "bars_thin": True,
                "last_close": None,
                "rsi14": None,
                "missing_for": list(BRAIN_CHECKERS.keys()),
                "stale": True,
                "source": None,
                "tf": None,
            })
            missing_snapshot_count += 1
            continue

        computed_at = _parse_iso(snap.get("computed_at"))
        snapshot_age_sec = (
            int((now - computed_at).total_seconds())
            if computed_at else None
        )
        indicators = snap.get("indicators") or {}
        bars_seen = indicators.get("bars_seen")
        if isinstance(bars_seen, (int, float)):
            bars_seen = int(bars_seen)
        else:
            bars_seen = None
        bars_thin = bars_seen is None or bars_seen < MIN_RELIABLE_BARS
        stale = (
            snapshot_age_sec is None
            or snapshot_age_sec > STALE_THRESHOLD_SEC
        )

        # Per-brain readiness — which brains *cannot* read this symbol
        missing_for: list[str] = []
        for brain_id, (_doctrine, checker) in BRAIN_CHECKERS.items():
            miss = checker(indicators)
            if miss:
                missing_for.append(brain_id)
            else:
                if not stale and not bars_thin:
                    evaluable_by_brain[brain_id] += 1

        if not stale:
            fresh_count += 1
        else:
            stale_count += 1
        if bars_thin:
            thin_bars_count += 1

        rows.append({
            "symbol": symbol,
            "has_snapshot": True,
            "snapshot_age_sec": snapshot_age_sec,
            "bars_seen": bars_seen,
            "bars_thin": bars_thin,
            "last_close": indicators.get("last_close"),
            "rsi14": indicators.get("rsi14"),
            "missing_for": missing_for,
            "stale": stale,
            "source": snap.get("source"),
            "tf": snap.get("tf"),
        })

    # ── per-brain rollup ──
    brain_rows = []
    for brain_id in BRAIN_CHECKERS:
        emit_stats = await _emit_stats_for_brain(brain_id, now)
        eval_count = evaluable_by_brain[brain_id]
        emit_stats["evaluable_count"] = eval_count
        emit_stats["evaluable_pct"] = (
            round(100.0 * eval_count / len(universe), 1)
            if universe else 0.0
        )
        brain_rows.append(emit_stats)

    # ── summary counts ──
    evaluable_all = sum(
        1 for r in rows
        if r["has_snapshot"]
        and not r["stale"]
        and not r["bars_thin"]
        and not r["missing_for"]
    )

    return {
        "ok": True,
        "as_of": now.isoformat(),
        "stale_threshold_sec": STALE_THRESHOLD_SEC,
        "min_reliable_bars": MIN_RELIABLE_BARS,
        "brains": brain_rows,
        "universe": rows,
        "summary": {
            "universe_size": len(universe),
            "fresh_count": fresh_count,
            "stale_count": stale_count,
            "missing_snapshot_count": missing_snapshot_count,
            "thin_bars_count": thin_bars_count,
            "evaluable_all_brains_count": evaluable_all,
            "evaluable_by_brain": evaluable_by_brain,
        },
    }
