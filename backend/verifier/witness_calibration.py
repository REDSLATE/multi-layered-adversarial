"""Witness calibration harness — read-only "what if?" scanner.

Doctrine pin (2026-02-19, operator directive):
    The resolver's promotion doctrine has three hardcoded knobs:
        RESOLUTION_HORIZON_HOURS          (default 24)
        MIN_MOVE_BPS_FOR_DIRECTIONAL_WIN  (default 50)
        HOLD_WINDOW_BPS                   (default 50)
    Different signal sources may earn power under different (horizon,
    threshold) combinations. News sentiment might resolve on a 6h
    horizon; SEC filings on 5 days. The default 24h/50bps is a
    starting point, not universal.

    This module answers "what would `source`'s win rate be at
    horizon=X, threshold=Y?" WITHOUT touching the ledger, without
    creating/rewriting `resolution_outcome` fields, and without
    mutating `external_source_credibility`. Pure advisory.

Two sweep modes:
    THRESHOLD SWEEP (fast — offline reclassification):
        Reads already-resolved rows (which have `resolution_return_bps`
        and `resolution_horizon_hours` persisted). Reclassifies at
        each candidate threshold against the row's ORIGINAL horizon.
        No bar refetch — milliseconds even for 10k+ rows.

    HORIZON SWEEP (slow — bar refetch required):
        For each candidate horizon that differs from the row's
        original resolved horizon, must refetch p1 at
        `bar_close_ts + horizon`. To keep this bounded, the horizon
        sweep uses a random sample (default N=500).

Doctrine anti-patterns this module WILL NOT do:
    * Mutate `external_source_credibility` — only the resolver
      (real, not calibration) may promote/demote.
    * Persist calibration outcomes onto witness rows — that would
      let the calibration harness contaminate the real ledger's
      view of which rows are "resolved."
    * Auto-select the "best" parameters. Trust by vibes. The
      operator reads the matrix and decides.
"""
from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from db import db
from namespaces import EXTERNAL_SIGNALS
from verifier.witness_resolver import (
    HOLD_WINDOW_BPS,
    MIN_MOVE_BPS_FOR_DIRECTIONAL_WIN,
    PriceFetcher,
    RESOLUTION_HORIZON_HOURS,
    classify_outcome,
)


logger = logging.getLogger("risedual.witness_calibrate")


@dataclass
class CalibrationCell:
    """One cell of the (horizon, threshold) matrix."""
    horizon_hours: int
    directional_threshold_bps: int
    hold_window_bps: int
    samples: int
    wins: int
    losses: int
    undetermined: int
    win_rate: float
    avg_return_bps: float
    would_promote_to_watchlist: bool  # ≥50 samples AND >50% win rate


def _classify_and_aggregate(
    return_bps_and_sides: List[tuple[str, float]],
    directional_threshold_bps: int,
    hold_window_bps: int,
) -> tuple[int, int, int, float, float]:
    """Pure helper — no I/O. Returns (samples, wins, losses,
    win_rate, avg_return_bps).

    Undetermined outcomes are counted separately by the caller.
    """
    samples = 0
    wins = 0
    losses = 0
    total_return_bps = 0.0
    for side, return_bps in return_bps_and_sides:
        outcome = classify_outcome(
            side, return_bps,
            directional_threshold_bps=directional_threshold_bps,
            hold_window_bps=hold_window_bps,
        )
        if outcome == "undetermined":
            continue
        samples += 1
        if outcome == "win":
            wins += 1
        else:
            losses += 1
        total_return_bps += return_bps
    win_rate = wins / samples if samples > 0 else 0.0
    avg_return_bps = total_return_bps / samples if samples > 0 else 0.0
    return samples, wins, losses, win_rate, avg_return_bps


async def calibrate_thresholds_offline(
    source: str,
    thresholds_bps: List[int],
    hold_window_bps: Optional[int] = None,
    limit: int = 10_000,
) -> Dict[str, Any]:
    """Offline threshold sweep over already-resolved rows.

    Reads rows where `resolution_return_bps` exists — no bar refetch.
    All returned cells share the ORIGINAL horizon at which the rows
    were resolved (typically 24h). Use `calibrate_horizons_sampled`
    for horizon sweeps.

    Args:
        source: witness source ("polygon", etc.)
        thresholds_bps: list of directional thresholds to try, e.g.
            [25, 50, 100, 200]
        hold_window_bps: HOLD window to use (default = same as
            directional threshold, matching the resolver's original
            configuration where both defaulted to 50).
        limit: max rows to sample from the resolved population.

    Returns a dict with `cells: list[CalibrationCell]` and metadata.
    Never writes anywhere. Read-only.
    """
    # Load resolved rows once — reuse across every threshold candidate.
    query = {
        "source": source,
        "resolution_return_bps": {"$exists": True},
        "side": {"$in": ["BUY", "SELL", "HOLD"]},
    }
    projection = {
        "_id": 0,
        "side": 1,
        "resolution_return_bps": 1,
        "resolution_horizon_hours": 1,
    }
    cursor = db[EXTERNAL_SIGNALS].find(query, projection).limit(limit)
    pop: List[tuple[str, float]] = []
    horizons_seen: dict[int, int] = {}
    async for row in cursor:
        side = row.get("side")
        rb = row.get("resolution_return_bps")
        h = row.get("resolution_horizon_hours")
        if side not in ("BUY", "SELL", "HOLD") or rb is None:
            continue
        pop.append((side, float(rb)))
        if h is not None:
            horizons_seen[int(h)] = horizons_seen.get(int(h), 0) + 1

    cells: List[dict] = []
    for thr in thresholds_bps:
        hw = hold_window_bps if hold_window_bps is not None else thr
        samples, wins, losses, wr, avg_bps = _classify_and_aggregate(
            pop, directional_threshold_bps=thr, hold_window_bps=hw,
        )
        # Undetermined can't come from resolved rows in this path
        # (classify_outcome only returns undetermined for unknown
        # side). Report zero for interface symmetry.
        undetermined = 0
        # Same 50-samples / 50%-win-rate rule the resolver uses to
        # promote UNTRUSTED → WATCHLIST. Advisory only; this endpoint
        # never actually promotes.
        would_promote = (samples >= 50 and wr > 0.50)
        cells.append({
            "horizon_hours": (
                # If all rows resolved at one horizon, report it;
                # else "mixed".
                list(horizons_seen.keys())[0]
                if len(horizons_seen) == 1
                else "mixed"
            ),
            "directional_threshold_bps": thr,
            "hold_window_bps": hw,
            "samples": samples,
            "wins": wins,
            "losses": losses,
            "undetermined": undetermined,
            "win_rate": round(wr, 4),
            "avg_return_bps": round(avg_bps, 2),
            "would_promote_to_watchlist": would_promote,
        })

    return {
        "source": source,
        "mode": "threshold_sweep_offline",
        "population_size": len(pop),
        "population_horizons": horizons_seen,
        "cells": cells,
        "doctrine": (
            "Offline threshold sweep. Reads stored resolution_return_bps "
            "from already-resolved rows — no bar refetch. All cells "
            "share the ORIGINAL horizon at which rows were resolved. "
            "Advisory only: does not mutate the credibility ledger."
        ),
    }


async def calibrate_horizons_sampled(
    source: str,
    horizons_hours: List[int],
    price_fetcher: PriceFetcher,
    directional_threshold_bps: int = MIN_MOVE_BPS_FOR_DIRECTIONAL_WIN,
    hold_window_bps: int = HOLD_WINDOW_BPS,
    sample_size: int = 500,
    seed: Optional[int] = None,
) -> Dict[str, Any]:
    """Horizon sweep — refetches p1 for each (row, horizon) pair.

    Reads witness rows regardless of resolution status; for each
    row, computes p0 at bar_close_ts and p1 at
    bar_close_ts + horizon, classifies at the fixed threshold, and
    aggregates per horizon.

    Bounded by `sample_size` (default 500 rows) because each row
    now costs O(len(horizons)) bar fetches. 500 × 4 horizons ×
    2 fetches ≈ 4000 fetches, which is a few seconds on the
    broker-primary bar cache.

    Never mutates external_signals or the credibility ledger.
    """
    now = datetime.now(timezone.utc)
    max_horizon = max(horizons_hours)
    cutoff_dt = now - timedelta(hours=max_horizon)
    cutoff = cutoff_dt.isoformat()

    # Only sample rows old enough that ALL candidate horizons could
    # have resolved. Filtering here avoids skew where longer horizons
    # only see the oldest rows.
    query = {
        "source": source,
        "side": {"$in": ["BUY", "SELL", "HOLD"]},
        "bar_close_ts": {"$lte": cutoff},
    }
    projection = {"_id": 0, "symbol": 1, "side": 1, "bar_close_ts": 1}

    total_eligible = await db[EXTERNAL_SIGNALS].count_documents(query)
    if total_eligible == 0:
        return {
            "source": source, "mode": "horizon_sweep_sampled",
            "population_size": 0, "sample_size": 0,
            "cells": [], "doctrine": "no eligible rows for these horizons",
        }

    # Reservoir sample via limit + skip. For pop < sample_size we
    # just take everything.
    take_all = total_eligible <= sample_size
    if take_all:
        cursor = db[EXTERNAL_SIGNALS].find(query, projection)
        rows: List[dict] = [r async for r in cursor]
    else:
        rng = random.Random(seed)
        skips = sorted(rng.sample(range(total_eligible), sample_size))
        # Batch-load, then pluck the chosen indices — one cursor pass,
        # random subset.
        cursor = db[EXTERNAL_SIGNALS].find(query, projection).limit(total_eligible)
        rows = []
        want = set(skips)
        for idx, row in enumerate([r async for r in cursor]):
            if idx in want:
                rows.append(row)
                if len(rows) >= sample_size:
                    break

    # For each horizon, compute return_bps per row, aggregate.
    cells: List[dict] = []
    for horizon in horizons_hours:
        return_bps_and_sides: List[tuple[str, float]] = []
        rows_skipped_price = 0
        rows_skipped_ts = 0
        for row in rows:
            try:
                bar_close_dt = datetime.fromisoformat(
                    str(row["bar_close_ts"]).replace("Z", "+00:00"),
                )
            except (TypeError, ValueError, KeyError):
                rows_skipped_ts += 1
                continue
            resolved_ts = (bar_close_dt + timedelta(hours=horizon)).isoformat()
            p0 = await price_fetcher(row["symbol"], bar_close_dt.isoformat())
            p1 = await price_fetcher(row["symbol"], resolved_ts)
            if p0 is None or p1 is None or p0 <= 0:
                rows_skipped_price += 1
                continue
            return_bps = ((p1 - p0) / p0) * 10_000.0
            return_bps_and_sides.append((row["side"], return_bps))

        samples, wins, losses, wr, avg_bps = _classify_and_aggregate(
            return_bps_and_sides,
            directional_threshold_bps=directional_threshold_bps,
            hold_window_bps=hold_window_bps,
        )
        would_promote = (samples >= 50 and wr > 0.50)
        cells.append({
            "horizon_hours": horizon,
            "directional_threshold_bps": directional_threshold_bps,
            "hold_window_bps": hold_window_bps,
            "samples": samples,
            "wins": wins,
            "losses": losses,
            "rows_skipped_price_missing": rows_skipped_price,
            "rows_skipped_bad_ts": rows_skipped_ts,
            "win_rate": round(wr, 4),
            "avg_return_bps": round(avg_bps, 2),
            "would_promote_to_watchlist": would_promote,
        })

    return {
        "source": source,
        "mode": "horizon_sweep_sampled",
        "population_size": total_eligible,
        "sample_size": len(rows),
        "sample_taken": "all" if take_all else "random",
        "cells": cells,
        "doctrine": (
            "Horizon sweep. For each candidate horizon this refetched "
            "p0 and p1 per sampled row — cost is O(sample × horizons). "
            "Fixed at the resolver's default directional/hold thresholds "
            "so the horizon effect is isolated. Advisory only: does "
            "not mutate the credibility ledger."
        ),
    }
