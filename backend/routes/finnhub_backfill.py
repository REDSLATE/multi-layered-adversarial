"""Historical OHLCV backfill from Finnhub.

Operator-triggered one-shot backfill of historical bars from Finnhub
for the S&P-500 universe. Distinct from the live polling worker
(`shared/feeders/finnhub_equity.py:_worker_loop`) which only pulls
the recent ~8h window every 5 minutes.

Why this exists (operator ask 2026-05-31):
  Brains need history to learn patterns (options trading, regime
  shifts, multi-year baselines). The Finnhub plan in use grants
  10 years of historical candles; this module operator-triggers
  the bulk pull.

Endpoints (mounted at `/api/admin/feeders/finnhub/backfill`):
  POST /symbol      Backfill ONE symbol (immediate, blocking, returns
                    counts). Useful for smoke-testing or filling a
                    gap on one ticker.
  POST /universe    Backfill the ENTIRE S&P-500 universe in the
                    background (returns job_id immediately).
  GET  /universe/{job_id}  Poll the universe backfill's progress.

Doctrine:
  - READ-ONLY broker path. Writes only to `shared_ohlcv_bars` with
    `source: "finnhub_equity"`. Idempotent on
    `(source, symbol, tf, ts)`.
  - Operator-only (JWT auth via `get_current_user`).
  - Throttled — Finnhub free tier is 60 calls/min, premium is
    300/min. We default to 30/min to leave headroom for the live
    polling worker.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query

from auth import get_current_user
from db import db
from namespaces import SHARED_OHLCV_BARS
from shared.feeders.feeder_health import record_feeder_health
from shared.feeders.finnhub_equity import (
    FEEDER_SOURCE,
    PROVIDER,
    _RES_TO_TF,
    candles_to_bars,
    fetch_candles,
)
from shared.snapshots.sp500_universe import SP500_TICKERS


logger = logging.getLogger("risedual.finnhub_backfill")
router = APIRouter(
    prefix="/admin/feeders/finnhub/backfill",
    tags=["feeders-backfill"],
)


# Default throttle: 30 calls/minute = 1 call every 2 seconds. Premium
# Finnhub allows 300/min so we leave 270/min for the live worker.
# Override with FINNHUB_BACKFILL_RPM env-var.
DEFAULT_BACKFILL_RPM = int(os.environ.get("FINNHUB_BACKFILL_RPM", "50"))

# 10 years is the published cap on the operator's plan.
DEFAULT_LOOKBACK_YEARS = 10

# In-process job state. One job_id → progress dict. Bounded by the
# operator's launch cadence; not persisted across restarts on purpose
# (the next-run picks up via idempotent upsert anyway).
_JOBS: Dict[str, Dict[str, Any]] = {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _persist_bar(bar: dict[str, Any]) -> None:
    """Same upsert key as the live ingest path. Idempotent on
    `(source, symbol, tf, ts)`."""
    key = {
        "source": bar["source"],
        "symbol": bar["symbol"],
        "tf": bar["tf"],
        "ts": bar["ts"],
    }
    bar["ingested_at"] = _now_iso()
    bar["ingested_via"] = "finnhub_backfill"
    await db[SHARED_OHLCV_BARS].update_one(
        key, {"$set": bar}, upsert=True,
    )


async def _persist_bars_bulk(bars: list[dict[str, Any]]) -> int:
    """Bulk upsert all bars for a symbol in one DB roundtrip. Same
    idempotency key as the live ingest path."""
    if not bars:
        return 0
    from pymongo import UpdateOne
    now_iso = _now_iso()
    ops = []
    for bar in bars:
        bar["ingested_at"] = now_iso
        bar["ingested_via"] = "finnhub_backfill"
        ops.append(UpdateOne(
            {
                "source": bar["source"],
                "symbol": bar["symbol"],
                "tf": bar["tf"],
                "ts": bar["ts"],
            },
            {"$set": bar},
            upsert=True,
        ))
    result = await db[SHARED_OHLCV_BARS].bulk_write(ops, ordered=False)
    return result.upserted_count + result.modified_count


async def backfill_one(
    symbol: str,
    resolution: str,
    api_key: str,
    *,
    lookback_years: int = DEFAULT_LOOKBACK_YEARS,
) -> Dict[str, Any]:
    """Backfill one symbol at one resolution. Returns counts.

    Finnhub honors arbitrary `from`/`to` ranges on /stock/candle —
    a single call covering 10 years of daily bars works (verified
    2026-05-31, returned 2,511 candles). For finer resolutions the
    payload still comes back in one response.
    """
    tf = _RES_TO_TF.get(resolution)
    if tf is None:
        return {
            "ok": False, "symbol": symbol, "resolution": resolution,
            "reason": f"unknown_resolution:{resolution}",
            "bars_persisted": 0,
        }
    now_ts = int(time.time())
    frm_ts = now_ts - lookback_years * 365 * 86_400
    payload = await fetch_candles(symbol, resolution, frm_ts, now_ts, api_key)
    if payload is None:
        return {
            "ok": False, "symbol": symbol, "resolution": resolution,
            "reason": "fetch_failed_see_feeder_health_audit",
            "bars_persisted": 0,
        }
    if payload.get("s") == "no_data":
        return {
            "ok": True, "symbol": symbol, "resolution": resolution,
            "reason": "no_data", "bars_persisted": 0,
        }
    bars = candles_to_bars(symbol, resolution, payload)
    try:
        persisted = await _persist_bars_bulk(bars)
    except Exception as exc:  # noqa: BLE001
        await record_feeder_health(
            provider=PROVIDER, endpoint="_persist_bars_bulk",
            status_code=None, error_type="db_error",
            message=f"{type(exc).__name__}: {exc}",
            context={"symbol": symbol, "tf": tf, "bar_count": len(bars)},
        )
        persisted = 0
    return {
        "ok": True, "symbol": symbol, "resolution": resolution, "tf": tf,
        "bars_returned": len(bars), "bars_persisted": persisted,
    }


# ──────────────────────── single-symbol endpoint ────────────────────────


@router.post("/symbol")
async def backfill_symbol(
    symbol: str = Query(..., description="Ticker, uppercase."),
    resolution: str = Query(
        "D",
        description=f"Finnhub resolution. One of {list(_RES_TO_TF.keys())}.",
    ),
    lookback_years: int = Query(
        DEFAULT_LOOKBACK_YEARS, ge=1, le=20,
        description="How far back to pull (default 10).",
    ),
    user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """Operator-triggered single-symbol backfill. Blocks until done.
    Daily over 10 years finishes in ~500ms per symbol."""
    api_key = (os.environ.get("FINNHUB_API_KEY") or "").strip()
    if not api_key:
        raise HTTPException(503, detail="FINNHUB_API_KEY not configured")
    if resolution not in _RES_TO_TF:
        raise HTTPException(
            400, detail=f"resolution must be one of {list(_RES_TO_TF.keys())}",
        )
    result = await backfill_one(
        symbol.upper(), resolution, api_key,
        lookback_years=lookback_years,
    )
    result["triggered_by"] = user.get("email")
    result["doctrine"] = "evidence_only"
    return result


# ──────────────────────── universe endpoint ────────────────────────


async def _universe_worker(
    job_id: str,
    universe: List[str],
    resolution: str,
    lookback_years: int,
    api_key: str,
    rpm: int,
) -> None:
    """Background worker — iterate universe with rate limiting."""
    job = _JOBS[job_id]
    interval = 60.0 / max(rpm, 1)
    for i, sym in enumerate(universe):
        if job.get("cancel_requested"):
            job["status"] = "cancelled"
            job["finished_at"] = _now_iso()
            return
        t0 = time.time()
        try:
            result = await backfill_one(
                sym, resolution, api_key, lookback_years=lookback_years,
            )
            job["symbols_processed"] = i + 1
            job["bars_persisted_total"] += result.get("bars_persisted", 0)
            if not result["ok"]:
                job["symbols_failed"].append(
                    {"symbol": sym, "reason": result.get("reason")},
                )
        except Exception as exc:  # noqa: BLE001
            job["symbols_failed"].append(
                {"symbol": sym, "reason": f"crash:{type(exc).__name__}"},
            )
            logger.warning("backfill crashed for %s: %s", sym, exc)
        # Rate-limit: ensure ≥interval seconds since loop start.
        elapsed = time.time() - t0
        if elapsed < interval:
            await asyncio.sleep(interval - elapsed)
    job["status"] = "complete"
    job["finished_at"] = _now_iso()


@router.post("/universe")
async def backfill_universe(
    background_tasks: BackgroundTasks,
    resolution: str = Query(
        "D",
        description=f"Finnhub resolution. One of {list(_RES_TO_TF.keys())}.",
    ),
    lookback_years: int = Query(
        DEFAULT_LOOKBACK_YEARS, ge=1, le=20,
    ),
    rpm: int = Query(
        DEFAULT_BACKFILL_RPM, ge=1, le=300,
        description="Calls-per-minute throttle.",
    ),
    universe_override: Optional[str] = Query(
        None,
        description="Optional comma-separated ticker list. Defaults to S&P 500.",
    ),
    user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """Operator-triggered S&P-500 backfill. Runs in the background;
    returns a job_id to poll for progress.

    Throughput math: 30 rpm × 502 symbols ≈ 16.7 min for the full
    universe at one resolution. Premium tier allows 300/min → ~2 min."""
    api_key = (os.environ.get("FINNHUB_API_KEY") or "").strip()
    if not api_key:
        raise HTTPException(503, detail="FINNHUB_API_KEY not configured")
    if resolution not in _RES_TO_TF:
        raise HTTPException(
            400, detail=f"resolution must be one of {list(_RES_TO_TF.keys())}",
        )
    if universe_override:
        universe = [
            s.strip().upper() for s in universe_override.split(",") if s.strip()
        ]
    else:
        universe = list(SP500_TICKERS)

    job_id = uuid.uuid4().hex
    _JOBS[job_id] = {
        "job_id": job_id,
        "status": "running",
        "started_at": _now_iso(),
        "finished_at": None,
        "universe_size": len(universe),
        "resolution": resolution,
        "tf": _RES_TO_TF.get(resolution),
        "lookback_years": lookback_years,
        "rpm": rpm,
        "estimated_seconds": int(len(universe) * (60.0 / max(rpm, 1))),
        "symbols_processed": 0,
        "symbols_failed": [],
        "bars_persisted_total": 0,
        "triggered_by": user.get("email"),
        "cancel_requested": False,
    }
    background_tasks.add_task(
        _universe_worker, job_id, universe, resolution, lookback_years,
        api_key, rpm,
    )
    return {
        "job_id": job_id,
        "status": "running",
        "universe_size": len(universe),
        "estimated_seconds": _JOBS[job_id]["estimated_seconds"],
        "poll_at": f"/api/admin/feeders/finnhub/backfill/universe/{job_id}",
        "doctrine": "evidence_only",
    }


@router.get("/universe/{job_id}")
async def get_backfill_status(
    job_id: str,
    _user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    job = _JOBS.get(job_id)
    if job is None:
        raise HTTPException(404, detail=f"job {job_id!r} not found")
    pct = (
        0 if job["universe_size"] == 0
        else int(100 * job["symbols_processed"] / job["universe_size"])
    )
    return {**job, "progress_pct": pct}


@router.post("/universe/{job_id}/cancel")
async def cancel_backfill(
    job_id: str,
    _user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    job = _JOBS.get(job_id)
    if job is None:
        raise HTTPException(404, detail=f"job {job_id!r} not found")
    if job["status"] != "running":
        return {"job_id": job_id, "status": job["status"], "cancelled": False}
    job["cancel_requested"] = True
    return {"job_id": job_id, "status": "running", "cancelled": True}
