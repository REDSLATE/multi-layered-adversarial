"""RTH Dynamic Opportunity Scanner (2026-07-24, operator spec).

Scanner chooses what is WORTH EXAMINING. Brains decide whether a
setup exists. Seat owns execution authority. Risk owns sizing.
This module is ADVISORY ONLY: it never emits intents, never sizes,
never touches order APIs — it nominates symbols into the candidate
cache, and the universe refresher merges the top discovery names into
`live_universe` for the MC pulse.

Design constraints honored:
  * Full discovery sweep in rotating budget CHUNKS (default 60
    symbols / 180s cycle → ~420-name universe sweeps in ~20 min)
    because Webull has no batch-quote API; bars fetched for scoring
    are persisted, so admitted candidates have history on arrival.
  * Candidates expire (default 15 min) and are hard-invalidated on
    stale data. Ranked pool lives in SQLite (hot path, no Atlas).
  * Hard exclusions: leveraged/inverse (policy override), price
    floor, min hourly dollar volume, min bar history, bar freshness.
  * Lightweight universe classification (etf / large_cap /
    growth_equity) recorded per candidate — doctrine-registry routing
    hook for the future options/doctrine work.
  * Per-brain affinity scores (barracuda/camino/hellcat/gto) computed
    from the same bars; brains stay free to HOLD.
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from shared.scanner import store
from shared.scanner.universe500 import ETFS, LEVERAGED_INVERSE, discovery_universe

logger = logging.getLogger("risedual.scanner")

INTERVAL_SEC = float(os.environ.get("SCANNER_INTERVAL_SEC", "180"))
CHUNK_SIZE = int(os.environ.get("SCANNER_CHUNK_SIZE", "60"))
CANDIDATE_TTL_MIN = float(os.environ.get("SCANNER_CANDIDATE_TTL_MIN", "15"))
MIN_SCORE = float(os.environ.get("SCANNER_MIN_SCORE", "0.35"))
TOP_N = int(os.environ.get("SCANNER_TOP_N", "25"))
_ET = ZoneInfo("America/New_York")

DEFAULT_POLICY = {
    "min_price": 5.0,
    "min_hourly_dollar_vol": 2_000_000.0,
    "min_bars": 20,
    "max_bar_age_min": 20.0,
    "allow_leveraged": False,
    "extra_symbols": [],
    "exclude_symbols": [],
}

_ETF_SET = set(ETFS)


async def get_policy() -> dict:
    try:
        from db import db  # noqa: WPS433
        doc = await db["runtime_flags"].find_one({"_id": "scanner_policy"}) or {}
    except Exception:  # noqa: BLE001
        doc = {}
    out = dict(DEFAULT_POLICY)
    for k in DEFAULT_POLICY:
        if k in doc and doc[k] is not None:
            out[k] = doc[k]
    return out


def is_rth(now: Optional[datetime] = None) -> bool:
    et = (now or datetime.now(timezone.utc)).astimezone(_ET)
    if et.weekday() >= 5:
        return False
    mins = et.hour * 60 + et.minute
    return 9 * 60 + 30 <= mins < 16 * 60


def classify(symbol: str, hourly_dollar_vol: float) -> str:
    if symbol in _ETF_SET:
        return "etf"
    if hourly_dollar_vol >= 20_000_000:
        return "large_cap"
    return "growth_equity"


# ── scoring (deterministic, from 5m bars) ──────────────────────────

def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def score_components(bars: list[dict]) -> Optional[dict]:
    """Component scores from chronological 5m bars (oldest→newest).
    Returns None when history is insufficient for honest scoring."""
    if len(bars) < 20:
        return None
    closes = [float(b["c"]) for b in bars]
    vols = [float(b["v"]) for b in bars]
    highs = [float(b["h"]) for b in bars]
    lows = [float(b["l"]) for b in bars]
    c = closes[-1]
    if c <= 0:
        return None

    base_vol = sum(vols[-30:-6]) / max(len(vols[-30:-6]), 1)
    recent_vol = sum(vols[-6:]) / 6.0
    rel_volume = recent_vol / base_vol if base_vol > 0 else 0.0

    hourly_dv = sum(v * cl for v, cl in zip(vols[-12:], closes[-12:]))
    momentum = c / closes[-13] - 1.0 if closes[-13] > 0 else 0.0

    accel_base = sum(vols[-12:-3]) / 9.0
    vol_accel = (sum(vols[-3:]) / 3.0) / accel_base if accel_base > 0 else 0.0

    session_open = closes[0]
    rel_strength = c / session_open - 1.0 if session_open > 0 else 0.0

    trs = [
        max(h - l, abs(h - pc), abs(l - pc))
        for h, l, pc in zip(highs[-12:], lows[-12:], closes[-13:-1])
    ]
    atr_pct = (sum(trs) / len(trs)) / c if trs else 0.0

    spread_quality = 1.0 if 5.0 <= c <= 800.0 else 0.5  # proxy: no L1 quote feed

    return {
        "relative_volume_score": _clamp(rel_volume / 3.0),
        "dollar_volume_score": _clamp(math.log10(hourly_dv + 1.0) / 9.0),
        "intraday_momentum_score": _clamp(abs(momentum) / 0.03),
        "volume_acceleration_score": _clamp(vol_accel / 2.5),
        "relative_strength_score": _clamp(abs(rel_strength) / 0.03),
        "volatility_opportunity_score": _clamp(atr_pct / 0.015),
        "spread_quality_score": spread_quality,
        "_price": c,
        "_hourly_dollar_vol": hourly_dv,
        "_momentum_signed": momentum,
        "_rel_strength_signed": rel_strength,
        "_rel_volume": rel_volume,
        "_atr_pct": atr_pct,
        "_closes": closes,
        "_highs": highs,
        "_lows": lows,
    }


def opportunity_score(comp: dict) -> float:
    return round(
        0.22 * comp["relative_volume_score"]
        + 0.18 * comp["dollar_volume_score"]
        + 0.16 * comp["intraday_momentum_score"]
        + 0.14 * comp["volume_acceleration_score"]
        + 0.12 * comp["relative_strength_score"]
        + 0.10 * comp["volatility_opportunity_score"]
        + 0.08 * comp["spread_quality_score"], 4,
    )


def brain_affinity(comp: dict) -> dict:
    """Personality match — advisory hints, never conviction."""
    closes = comp["_closes"]
    c = closes[-1]
    n20 = closes[-20:]
    sma20 = sum(n20) / len(n20)
    var = sum((x - sma20) ** 2 for x in n20) / len(n20)
    std20 = math.sqrt(var) if var > 0 else 0.0
    z = (c - sma20) / std20 if std20 > 0 else 0.0

    liquidity = comp["dollar_volume_score"]
    # Barracuda: stretched from mean + liquid enough to fade.
    barracuda = _clamp(abs(z) / 2.5) * 0.7 + liquidity * 0.3
    # Camino: orderly trend — price above rising mean, steady strength.
    sma_early = sum(closes[-20:-10]) / 10.0
    trend_up = 1.0 if (c > sma20 > sma_early) or (c < sma20 < sma_early) else 0.0
    camino = trend_up * 0.5 + comp["relative_strength_score"] * 0.3 + liquidity * 0.2
    # Hellcat: compression → expansion near session extremes.
    rng_recent = max(comp["_highs"][-6:]) - min(comp["_lows"][-6:])
    rng_session = max(comp["_highs"]) - min(comp["_lows"])
    compression = 1.0 - _clamp(rng_recent / rng_session if rng_session > 0 else 1.0)
    near_high = _clamp(1.0 - (max(comp["_highs"]) - c) / c / 0.01)
    hellcat = compression * 0.4 + near_high * 0.35 + comp["volume_acceleration_score"] * 0.25
    # GTO: raw momentum + relative volume.
    gto = (
        comp["intraday_momentum_score"] * 0.45
        + comp["relative_volume_score"] * 0.35
        + comp["volatility_opportunity_score"] * 0.20
    )
    return {
        "barracuda": round(_clamp(barracuda), 3),
        "camino": round(_clamp(camino), 3),
        "hellcat": round(_clamp(hellcat), 3),
        "gto": round(_clamp(gto), 3),
    }


def evaluate_symbol(
    symbol: str, bars: list[dict], policy: dict,
    now: Optional[datetime] = None,
) -> tuple[Optional[dict], Optional[str]]:
    """(candidate, rejection_reason) — exactly one is non-None."""
    now = now or datetime.now(timezone.utc)
    if not policy.get("allow_leveraged") and symbol in LEVERAGED_INVERSE:
        return None, "leveraged_inverse"
    if len(bars) < int(policy["min_bars"]):
        return None, "insufficient_bars"
    try:
        last_ts = datetime.fromisoformat(str(bars[-1]["ts"]))
        if last_ts.tzinfo is None:
            last_ts = last_ts.replace(tzinfo=timezone.utc)
    except Exception:  # noqa: BLE001
        return None, "bad_bar_timestamp"
    age_min = (now - last_ts).total_seconds() / 60.0
    if age_min > float(policy["max_bar_age_min"]):
        return None, "stale_data"
    comp = score_components(bars)
    if comp is None:
        return None, "insufficient_bars"
    if comp["_price"] < float(policy["min_price"]):
        return None, "below_min_price"
    if comp["_hourly_dollar_vol"] < float(policy["min_hourly_dollar_vol"]):
        return None, "low_dollar_volume"
    score = opportunity_score(comp)
    if score < MIN_SCORE:
        return None, "below_min_score"
    public = {k: v for k, v in comp.items() if not k.startswith("_")}
    return {
        "symbol": symbol,
        "opportunity_score": score,
        "components": public,
        "brain_affinity": brain_affinity(comp),
        "classification": classify(symbol, comp["_hourly_dollar_vol"]),
        "price": round(comp["_price"], 4),
        "hourly_dollar_vol": round(comp["_hourly_dollar_vol"], 0),
        "momentum_pct": round(comp["_momentum_signed"] * 100, 3),
        "bar_age_min": round(age_min, 1),
        "scanned_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=CANDIDATE_TTL_MIN)).isoformat(),
        "inclusion_reason": "scored_admission",
    }, None


# ── scan loop ───────────────────────────────────────────────────────

_state: dict[str, Any] = {
    "running": False, "task": None, "cursor": 0,
    "last_scan_at": None, "last_scan": None, "scans": 0,
}


async def _fetch_bars(symbol: str, count: int = 40) -> list[dict]:
    """5m bars via the same SDK path the feeder uses; persisted so
    admitted candidates arrive with history for the pulse."""
    from db import db  # noqa: WPS433
    from namespaces import SHARED_OHLCV_BARS  # noqa: WPS433
    from shared.feeders.webull_ohlc import _fetch_and_persist_one  # noqa: WPS433
    try:
        await _fetch_and_persist_one(symbol, "5m", count)
    except Exception:  # noqa: BLE001
        pass
    try:
        rows = await db[SHARED_OHLCV_BARS].find(
            {"symbol": symbol.upper(), "tf": "5m"}, {"_id": 0},
        ).sort("ts", -1).limit(count).to_list(count)
        return sorted(rows, key=lambda b: b["ts"])
    except Exception:  # noqa: BLE001
        return []


async def scan_once(force: bool = False) -> dict:
    """One budget-chunked scan cycle. Advisory only."""
    now = datetime.now(timezone.utc)
    if not force and not is_rth(now) and not _env_true("SCANNER_IGNORE_RTH"):
        return {"skipped": "outside_rth"}
    policy = await get_policy()
    universe = discovery_universe(policy)
    if not universe:
        return {"skipped": "empty_universe"}

    start = _state["cursor"] % len(universe)
    chunk = [universe[(start + i) % len(universe)] for i in range(min(CHUNK_SIZE, len(universe)))]
    _state["cursor"] = (start + len(chunk)) % len(universe)

    rejects: dict[str, int] = {}
    admitted: list[dict] = []
    for sym in chunk:
        bars = await _fetch_bars(sym)
        cand, why = evaluate_symbol(sym, bars, policy, now=now)
        if cand:
            admitted.append(cand)
        else:
            rejects[why] = rejects.get(why, 0) + 1

    store.upsert_candidates(admitted)
    purged = store.purge_expired()

    # Publish: refresher re-merges pins + core + top discovery into
    # live_universe so the pulse sees fresh candidates within one cycle.
    published = False
    try:
        from shared.universe.refresher import refresh_equity_universe  # noqa: WPS433
        await refresh_equity_universe()
        published = True
    except Exception as exc:  # noqa: BLE001
        logger.warning("scanner→universe publish failed: %s", exc)

    summary = {
        "universe_size": len(universe),
        "chunk_scanned": len(chunk),
        "cursor": _state["cursor"],
        "admitted": len(admitted),
        "rejects": rejects,
        "purged_expired": purged,
        "pool_live": store.status()["live"],
        "published_to_universe": published,
        "at": now.isoformat(),
    }
    _state["last_scan_at"] = now.isoformat()
    _state["last_scan"] = summary
    _state["scans"] += 1
    logger.info("scanner cycle: %s", summary)
    return summary


def _env_true(key: str) -> bool:
    return (os.environ.get(key) or "").strip().lower() in ("1", "true", "yes", "on")


def get_status() -> dict:
    return {
        "running": _state["running"],
        "interval_sec": INTERVAL_SEC,
        "chunk_size": CHUNK_SIZE,
        "candidate_ttl_min": CANDIDATE_TTL_MIN,
        "min_score": MIN_SCORE,
        "top_n": TOP_N,
        "rth_now": is_rth(),
        "scans": _state["scans"],
        "last_scan_at": _state["last_scan_at"],
        "last_scan": _state["last_scan"],
        "store": store.status(),
    }


async def _loop() -> None:
    logger.info("rth scanner loop start interval=%.0fs chunk=%d", INTERVAL_SEC, CHUNK_SIZE)
    while True:
        try:
            await scan_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("scanner tick failed: %s", exc)
        await asyncio.sleep(INTERVAL_SEC)


def start_if_enabled() -> None:
    if (os.environ.get("SCANNER_ENABLED") or "true").strip().lower() in (
        "0", "false", "no", "off",
    ):
        logger.info("rth scanner disabled via SCANNER_ENABLED")
        return
    if _state.get("running"):
        return
    task = asyncio.get_event_loop().create_task(_loop(), name="rth_scanner")
    _state.update(running=True, task=task)


async def stop() -> None:
    task = _state.get("task")
    if task and not task.done():
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    _state.update(running=False, task=None)
