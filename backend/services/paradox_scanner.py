"""
Paradox Coordinator v0 — Scanner service.

Doctrine pin (2026-02-XX):
    Scan PRODUCES `paradox_candidates`. It does NOT produce trade
    intents. It does NOT post to `/api/execution/submit`. Every
    candidate is admin-promoted (or auto-promoted by the human-in-
    the-loop policy) before any execution attempt is made.

    Filters (per user v0 spec):
        price >= 2
        volume >= 500_000
        spread_bps <= 75
        rvol >= 1.5
        halted is False

Universe:
    1. PRIMARY:  `paradox_watchlist` rows where `active=True`.
    2. FALLBACK: hardcoded top-liquid list (when watchlist is empty).
    3. LATER:    Alpaca screener / news / Alpha Vantage (not v0).

Inputs:
    snapshots: optional dict {SYMBOL: {price, volume, spread_bps,
               rvol, halted}}. When omitted, candidates land with
               status="pending_snapshot" so the operator (or a
               brain sidecar) can fill them later. This keeps the
               scanner from inventing market data.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from db import db
from namespaces import PARADOX_CANDIDATES, PARADOX_WATCHLIST


# Hardcoded top-liquid fallback universe used when watchlist is empty.
FALLBACK_UNIVERSE: List[Dict[str, str]] = [
    {"symbol": "SPY",   "lane": "equity"},
    {"symbol": "QQQ",   "lane": "equity"},
    {"symbol": "AAPL",  "lane": "equity"},
    {"symbol": "MSFT",  "lane": "equity"},
    {"symbol": "NVDA",  "lane": "equity"},
    {"symbol": "TSLA",  "lane": "equity"},
    {"symbol": "AMZN",  "lane": "equity"},
    {"symbol": "META",  "lane": "equity"},
    {"symbol": "GOOGL", "lane": "equity"},
    {"symbol": "AMD",   "lane": "equity"},
]


# Filter thresholds — pinned by user spec. Tripwire locks these.
FILTER_PRICE_MIN = 2.0
FILTER_VOLUME_MIN = 500_000
FILTER_SPREAD_BPS_MAX = 75.0
FILTER_RVOL_MIN = 1.5


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _resolve_universe() -> List[Dict[str, str]]:
    """Pull active watchlist rows; fall back to hardcoded list."""
    rows: List[Dict[str, str]] = []
    async for d in db[PARADOX_WATCHLIST].find(
        {"active": True}, {"_id": 0, "symbol": 1, "lane": 1},
    ):
        rows.append({"symbol": d["symbol"], "lane": d.get("lane", "equity")})
    if rows:
        return rows
    return list(FALLBACK_UNIVERSE)


def _apply_filters(snapshot: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Return {pass: bool, failures: list[str], reason: str}.
    A missing snapshot is treated as 'pending_snapshot' — NOT a
    pass, NOT a hard fail. The coordinator can re-scan once a
    snapshot lands."""
    if not snapshot:
        return {"pass": False, "failures": ["no_snapshot"], "reason": "pending_snapshot"}
    failures: List[str] = []
    if snapshot.get("halted") is True:
        failures.append("halted")
    price = _to_float(snapshot.get("price"))
    if price is None or price < FILTER_PRICE_MIN:
        failures.append("price_below_min")
    volume = _to_float(snapshot.get("volume"))
    if volume is None or volume < FILTER_VOLUME_MIN:
        failures.append("volume_below_min")
    spread = _to_float(snapshot.get("spread_bps"))
    if spread is None or spread > FILTER_SPREAD_BPS_MAX:
        failures.append("spread_above_max")
    rvol = _to_float(snapshot.get("rvol"))
    if rvol is None or rvol < FILTER_RVOL_MIN:
        failures.append("rvol_below_min")
    if failures:
        return {"pass": False, "failures": failures,
                "reason": "filter_failed: " + ", ".join(failures)}
    return {
        "pass": True,
        "failures": [],
        "reason": "high rvol + acceptable spread" if rvol and rvol >= 2.0
                  else "filters_passed",
    }


def _to_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


async def run_scan(
    *,
    snapshots: Optional[Dict[str, Dict[str, Any]]] = None,
    universe_override: Optional[List[Dict[str, str]]] = None,
    source: str = "paradox_scan_v0",
) -> Dict[str, Any]:
    """Walk the universe, apply filters, persist candidates.

    Returns a summary {candidates: list, summary: {...}}.
    Each persisted candidate has its own `candidate_id` so the
    evaluator can reference it.
    """
    snapshots = snapshots or {}
    universe = list(universe_override) if universe_override is not None else await _resolve_universe()

    candidates: List[Dict[str, Any]] = []
    pass_count = 0
    pending_count = 0
    fail_count = 0
    now = _now()

    for entry in universe:
        symbol = (entry.get("symbol") or "").upper().strip()
        lane = (entry.get("lane") or "equity").lower()
        if not symbol:
            continue
        snap = snapshots.get(symbol) or snapshots.get(symbol.lower())
        verdict = _apply_filters(snap)
        if verdict["pass"]:
            status = "candidate"
            pass_count += 1
        elif verdict["reason"] == "pending_snapshot":
            status = "pending_snapshot"
            pending_count += 1
        else:
            status = "filtered_out"
            fail_count += 1

        doc = {
            "candidate_id": str(uuid.uuid4()),
            "symbol": symbol,
            "lane": lane,
            "source": source,
            "status": status,
            "reason": verdict["reason"],
            "filter_pass": verdict["pass"],
            "filter_failures": verdict["failures"],
            "snapshot": snap or {},
            "created_at": now,
            "evaluated_at": None,
            "evaluation_id": None,
        }
        # Persist only `candidate` and `pending_snapshot` — drop
        # `filtered_out` to keep the collection from accumulating
        # noise. The summary still reports them.
        if status in ("candidate", "pending_snapshot"):
            await db[PARADOX_CANDIDATES].insert_one(dict(doc))
            candidates.append(_serialize(doc))

    return {
        "ok": True,
        "summary": {
            "universe_size": len(universe),
            "candidates": pass_count,
            "pending_snapshot": pending_count,
            "filtered_out": fail_count,
        },
        "candidates": candidates,
        "ts": now.isoformat(),
    }


def _serialize(doc: Dict[str, Any]) -> Dict[str, Any]:
    doc.pop("_id", None)
    for k in ("created_at", "evaluated_at"):
        v = doc.get(k)
        if isinstance(v, datetime):
            doc[k] = v.isoformat()
    return doc
