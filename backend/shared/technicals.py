"""Shared technical-evidence layer.

Doctrine:
    OHLCV bars are shared evidence. Indicators are deterministic functions
    of those bars. All four brains read the same series; each forms its
    own opinion. No brain owns the feed.

Write path: external feeder sidecars (Kraken Pro for crypto, ThinkOrSwim
for other markets, or a manual feeder during backfill) POST normalized
bars to `/api/ingest/ohlcv` using an `X-Feeder-Token`. Each feeder has
its own token in the .env so we can revoke one without disturbing others.

Read path:
    - Operators read via `/api/shared/technical/...` (JWT).
    - Brains read via `/api/runtime-discussion/technical/...`
      (X-Runtime-Token=<runtime>) — same payload, runtime-scoped auth.

Doctrine guards:
    - The feed cannot carry execution authority. Schema rejects any
      `may_execute` field.
    - Bars are append-only by (source, symbol, tf, ts). Re-ingest replaces
      the bar (revisions happen) and recomputes that bar's snapshot.
    - There is no DELETE endpoint here. Corrections come via re-ingest.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

from auth import get_current_user
from db import db
from namespaces import (
    DISCUSSION_PARTICIPANTS,
    SHARED_INDICATOR_SNAPSHOTS,
    SHARED_OHLCV_BARS,
)
from runtime_auth import verify_runtime_token
from shared.indicators import build_snapshot


# ──────────────────────── config ────────────────────────

# How many bars do we keep in the rolling window for indicator computation?
# Largest SMA we compute is 200; keep a healthy buffer so MACD/RSI Wilder
# smoothing converges cleanly when bars trickle in.
SNAPSHOT_LOOKBACK_BARS = 300

# Supported timeframes — fixed to keep storage predictable. Easy to
# extend later if a feeder needs a custom tf.
TIMEFRAMES = ("1m", "5m", "15m", "1h", "4h", "1d")

# Feeder identities. Each maps to an env-var token. Adding a new feeder
# is a 2-line change (here + .env).
FEEDERS: dict[str, str] = {
    "kraken_pro":   "KRAKEN_FEEDER_TOKEN",
    "thinkorswim":  "TOS_FEEDER_TOKEN",
    "manual":       "MANUAL_FEEDER_TOKEN",   # optional; for backfill
}

# Symbol shape — uppercase alnum, optional slash for crypto pairs
# (BTC/USD, ETH/USD), optional dot for some equity tickers (BRK.B).
import re as _re
_SYMBOL_RE = _re.compile(r"^[A-Z0-9][A-Z0-9./_-]{0,31}$")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _verify_feeder(source: str, token: str | None) -> None:
    if source not in FEEDERS:
        raise HTTPException(
            status_code=400,
            detail=f"source must be one of {tuple(FEEDERS)}",
        )
    env_key = FEEDERS[source]
    expected = os.environ.get(env_key)
    if not expected:
        raise HTTPException(
            status_code=503,
            detail=f"feeder token for {source} is not configured",
        )
    if not token or token != expected:
        raise HTTPException(status_code=401, detail="invalid feeder token")


# ──────────────────────── ingest ────────────────────────

class OHLCVBarIn(BaseModel):
    """A single OHLCV bar from a feeder sidecar."""
    source: Literal["kraken_pro", "thinkorswim", "manual"]
    symbol: str = Field(..., min_length=1, max_length=32)
    tf: Literal["1m", "5m", "15m", "1h", "4h", "1d"]
    ts: str = Field(..., description="ISO 8601 bar-open timestamp, UTC")
    o: float
    h: float
    l: float  # noqa: E741 — domain shorthand for "low"
    c: float
    v: float = Field(0.0, ge=0.0)

    @field_validator("symbol")
    @classmethod
    def _symbol_format(cls, v: str) -> str:
        v = v.upper()
        if not _SYMBOL_RE.match(v):
            raise ValueError(
                "symbol must be uppercase alnum + ./_- (e.g. BTC/USD, NVDA, BRK.B)"
            )
        return v

    @field_validator("ts")
    @classmethod
    def _ts_format(cls, v: str) -> str:
        # Accept any ISO 8601 the brain happens to send; normalize to UTC.
        try:
            dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError as e:
            raise ValueError(f"ts must be ISO 8601: {e}") from e
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()


class OHLCVBatchIn(BaseModel):
    """Convenience batch endpoint — feeders can stream a backfill window."""
    bars: list[OHLCVBarIn] = Field(..., min_length=1, max_length=2000)


router = APIRouter(tags=["technicals"])


async def _persist_bar(bar: dict) -> None:
    """Idempotent upsert of one bar, keyed by (source, symbol, tf, ts)."""
    key = {
        "source": bar["source"],
        "symbol": bar["symbol"],
        "tf": bar["tf"],
        "ts": bar["ts"],
    }
    bar["ingested_at"] = _now_iso()
    await db[SHARED_OHLCV_BARS].update_one(
        key,
        {"$set": bar},
        upsert=True,
    )


async def _recompute_snapshot(source: str, symbol: str, tf: str) -> dict:
    """Pull the most recent SNAPSHOT_LOOKBACK_BARS for (source,symbol,tf)
    and rebuild the indicator snapshot from scratch.

    Stored as one doc per (source, symbol, tf) — we only ever keep the
    latest. Historical replay is achieved by recomputing from the raw
    bars (which ARE retained).
    """
    bars = await db[SHARED_OHLCV_BARS].find(
        {"source": source, "symbol": symbol, "tf": tf},
        {"_id": 0},
    ).sort("ts", -1).to_list(SNAPSHOT_LOOKBACK_BARS)
    bars.reverse()  # ascending for indicator math
    snap = build_snapshot(bars)
    doc = {
        "source": source,
        "symbol": symbol,
        "tf": tf,
        "last_bar_ts": bars[-1]["ts"] if bars else None,
        "computed_at": _now_iso(),
        "indicators": snap,
    }
    await db[SHARED_INDICATOR_SNAPSHOTS].update_one(
        {"source": source, "symbol": symbol, "tf": tf},
        {"$set": doc},
        upsert=True,
    )
    return doc


@router.post("/ingest/ohlcv")
async def post_bar(
    bar: OHLCVBarIn,
    x_feeder_token: str | None = Header(default=None, alias="X-Feeder-Token"),
):
    """Single-bar ingest. Idempotent."""
    _verify_feeder(bar.source, x_feeder_token)
    await _persist_bar(bar.model_dump())
    snap = await _recompute_snapshot(bar.source, bar.symbol, bar.tf)
    return {
        "ok": True,
        "source": bar.source,
        "symbol": bar.symbol,
        "tf": bar.tf,
        "ts": bar.ts,
        "snapshot_ready": snap["indicators"].get("ready", False),
        "bars_seen": snap["indicators"].get("bars_seen", 0),
    }


@router.post("/ingest/ohlcv/batch")
async def post_bars_batch(
    body: OHLCVBatchIn,
    x_feeder_token: str | None = Header(default=None, alias="X-Feeder-Token"),
):
    """Batch ingest. All bars must come from the same source (verified
    against the supplied feeder token). Snapshots are recomputed once per
    affected (symbol, tf) pair at the end."""
    if not body.bars:
        raise HTTPException(status_code=400, detail="empty batch")
    sources = {b.source for b in body.bars}
    if len(sources) != 1:
        raise HTTPException(
            status_code=400, detail="all bars in a batch must share the same source",
        )
    source = next(iter(sources))
    _verify_feeder(source, x_feeder_token)

    affected: set[tuple[str, str]] = set()
    for b in body.bars:
        await _persist_bar(b.model_dump())
        affected.add((b.symbol, b.tf))

    snapshots: list[dict] = []
    for symbol, tf in affected:
        snap = await _recompute_snapshot(source, symbol, tf)
        snapshots.append({
            "symbol": symbol, "tf": tf,
            "ready": snap["indicators"].get("ready", False),
            "bars_seen": snap["indicators"].get("bars_seen", 0),
        })
    return {"ok": True, "ingested": len(body.bars), "snapshots": snapshots}


# ──────────────────────── read (operator) ────────────────────────

def _preferred_source(rows: list[dict]) -> str | None:
    """If multiple feeders cover the same (symbol, tf), prefer
    kraken_pro (live crypto) then thinkorswim then manual."""
    if not rows:
        return None
    order = {"kraken_pro": 0, "thinkorswim": 1, "manual": 2}
    rows = sorted(rows, key=lambda r: order.get(r.get("source", ""), 99))
    return rows[0]["source"]


@router.get("/shared/technical/symbols")
async def list_symbols(
    _user: dict = Depends(get_current_user),
):
    """Returns every (source, symbol, tf) currently covered by the feed,
    plus the latest bar timestamp for each. Used by the Mission Control
    overview panel to render the universe.
    """
    pipeline = [
        {"$group": {
            "_id": {"source": "$source", "symbol": "$symbol", "tf": "$tf"},
            "last_bar_ts": {"$max": "$ts"},
            "bars": {"$sum": 1},
        }},
        {"$project": {
            "_id": 0,
            "source": "$_id.source",
            "symbol": "$_id.symbol",
            "tf": "$_id.tf",
            "last_bar_ts": 1,
            "bars": 1,
        }},
        {"$sort": {"last_bar_ts": -1}},
    ]
    docs = await db[SHARED_OHLCV_BARS].aggregate(pipeline).to_list(2000)
    return {"items": docs, "count": len(docs)}


@router.get("/shared/technical/feeders")
async def list_feeders(
    _user: dict = Depends(get_current_user),
):
    """Per-feeder status overview for the Mission Control feeders strip.

    For each configured feeder (kraken_pro / thinkorswim / manual), returns:
      - configured: is the env-var token set?
      - status: live | stale | awaiting | unconfigured
      - last_bar_ts: most recent bar timestamp this feeder has produced
      - symbols: list of symbols this feeder is feeding (capped at 20)
      - bars_count: total bars stored from this feeder
    Live/stale threshold is tf-aware — a 1h feed that hasn't ingested in
    24h is "stale"; a 1d feed gets a 48h grace period.
    """
    from datetime import datetime, timezone, timedelta

    now = datetime.now(timezone.utc)

    # Group bars by source to get last_bar_ts + symbol coverage.
    pipeline = [
        {"$group": {
            "_id": "$source",
            "last_bar_ts": {"$max": "$ts"},
            "bars_count": {"$sum": 1},
            "symbols": {"$addToSet": "$symbol"},
            "tfs": {"$addToSet": "$tf"},
        }},
    ]
    agg = await db[SHARED_OHLCV_BARS].aggregate(pipeline).to_list(50)
    by_source = {a["_id"]: a for a in agg}

    items: list[dict] = []
    for key, env_key in FEEDERS.items():
        configured = bool(os.environ.get(env_key))
        info = by_source.get(key)

        if info:
            last_ts_str = info["last_bar_ts"]
            try:
                last_ts = datetime.fromisoformat(last_ts_str.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                last_ts = None
            tfs = set(info.get("tfs", []))
            # Pick the most permissive stale threshold — if the feeder is
            # carrying daily bars, give a 48h window; otherwise 24h.
            stale_after = timedelta(hours=48 if "1d" in tfs else 24)
            age = (now - last_ts) if last_ts else None
            if age is None:
                status = "unknown"
            elif age <= timedelta(hours=2 if "1d" not in tfs else 26):
                status = "live"
            elif age <= stale_after:
                status = "fresh"
            else:
                status = "stale"
            symbols = sorted(info.get("symbols", []))[:20]
            bars_count = info["bars_count"]
        else:
            status = "awaiting" if configured else "unconfigured"
            last_ts_str = None
            symbols = []
            bars_count = 0

        items.append({
            "key": key,
            "env_key": env_key,
            "configured": configured,
            "status": status,
            "last_bar_ts": last_ts_str,
            "symbols": symbols,
            "symbols_count": len(symbols),
            "bars_count": bars_count,
            "tfs": sorted(info.get("tfs", [])) if info else [],
        })
    return {"items": items, "endpoint": "/api/ingest/ohlcv"}


@router.get("/shared/technical/{symbol:path}")
async def get_technical(
    symbol: str,
    tf: str = Query("1h"),
    source: Optional[str] = Query(None, description="kraken_pro|thinkorswim|manual"),
    bars: int = Query(50, ge=1, le=300),
    _user: dict = Depends(get_current_user),
):
    return await _read_technical(symbol.upper(), tf, source, bars)


# ──────────────────────── read (runtime) ────────────────────────

@router.get("/runtime-discussion/technical/{symbol:path}")
async def runtime_get_technical(
    symbol: str,
    runtime_caller: str = Query(..., alias="caller"),
    tf: str = Query("1h"),
    source: Optional[str] = Query(None),
    bars: int = Query(50, ge=1, le=300),
    x_runtime_token: str | None = Header(default=None, alias="X-Runtime-Token"),
):
    """Same payload as the operator endpoint, but runtime-token auth so
    sidecars can pull without an operator JWT. Identical shape ⇒ brains
    can include `evidence.technical_ref` in their opinions referencing
    the snapshot they read (replayable audit)."""
    verify_runtime_token(runtime_caller, x_runtime_token or "")
    if runtime_caller not in DISCUSSION_PARTICIPANTS:
        raise HTTPException(
            status_code=400,
            detail=f"caller must be one of {DISCUSSION_PARTICIPANTS}",
        )
    return await _read_technical(symbol.upper(), tf, source, bars)


async def _read_technical(symbol: str, tf: str, source: Optional[str], bars_n: int) -> dict:
    if tf not in TIMEFRAMES:
        raise HTTPException(status_code=400, detail=f"tf must be one of {TIMEFRAMES}")

    # Resolve source: explicit, else pick the preferred feeder that covers
    # this (symbol, tf).
    if source is None:
        covering = await db[SHARED_OHLCV_BARS].find(
            {"symbol": symbol, "tf": tf},
            {"_id": 0, "source": 1},
        ).to_list(50)
        source = _preferred_source(covering)
        if source is None:
            raise HTTPException(
                status_code=404,
                detail=f"no bars for {symbol} {tf}",
            )
    elif source not in FEEDERS:
        raise HTTPException(status_code=400, detail=f"source must be one of {tuple(FEEDERS)}")

    snap = await db[SHARED_INDICATOR_SNAPSHOTS].find_one(
        {"source": source, "symbol": symbol, "tf": tf}, {"_id": 0},
    )
    if not snap:
        raise HTTPException(
            status_code=404, detail=f"no snapshot for {symbol} {tf} via {source}",
        )

    tail = await db[SHARED_OHLCV_BARS].find(
        {"source": source, "symbol": symbol, "tf": tf}, {"_id": 0},
    ).sort("ts", -1).to_list(bars_n)
    tail.reverse()

    return {
        "source": source,
        "symbol": symbol,
        "tf": tf,
        "bars": tail,
        "snapshot": snap,
        "doctrine": (
            "Shared technical evidence. Same bars, four brains, four "
            "interpretations. No execution authority is conveyed here."
        ),
    }
