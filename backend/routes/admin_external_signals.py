"""Witnesses Diagnostic — read-only "Untrusted Witnesses" panel.

Doctrine pin (2026-02-23, witness-council layer):
    External signals (Polygon news+sentiment, eventually Pine /
    Public / MTR) land in the `external_signals` holding cell as
    DEFAULT-HOSTILE: `verifier_status=UNTRUSTED`, `influence_allowed=False`.
    Nothing they write affects a trade. This endpoint is the
    operator's window into what witnesses are saying — read-only,
    no click-to-execute path, no mutation surface.

    Doctrine frame: TRIAL COURT, NOT A VOTING SYSTEM.
    Pine / Polygon / Public are witnesses, not authorities.
    Verifier (future) decides if any source earns weight.
    Until then, witness output is for operator situational
    awareness only.

Endpoints:
    GET  /api/admin/external-signals              recent witness rows
    GET  /api/admin/external-signals/credibility  per-source case files

Auth: operator JWT.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from auth import get_current_user
from db import db
from namespaces import EXTERNAL_SIGNALS, EXTERNAL_SOURCE_CREDIBILITY


router = APIRouter(tags=["admin"])


_PROJECTION_RECENT = {
    "_id": 0,
    "id": 1,
    "source": 1,
    "symbol": 1,
    "side": 1,
    "self_reported_confidence": 1,
    "event": 1,
    "reason": 1,
    "bar_close_ts": 1,
    "verifier_status": 1,
    "influence_allowed": 1,
    "received_at": 1,
}


@router.get("/admin/external-signals")
async def list_external_signals(
    _user=Depends(get_current_user),
    source: Optional[str] = Query(default=None, description="filter by witness source"),
    symbol: Optional[str] = Query(default=None, description="filter by ticker"),
    side: Optional[str] = Query(default=None, description="filter BUY/SELL/HOLD"),
    hours: int = Query(default=24, ge=1, le=720, description="lookback window"),
    limit: int = Query(default=100, ge=1, le=500),
):
    """Return recent witness rows for the Untrusted Witnesses panel.

    Read-only. No mutation, no execution side-effects. The panel
    renders these as dimmed cards with a status badge that
    explicitly says "UNTRUSTED — no execution influence" so the
    operator never confuses a witness alert for a brain signal.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    q: dict[str, Any] = {"received_at": {"$gte": cutoff}}
    if source:
        q["source"] = source.strip().lower()
    if symbol:
        q["symbol"] = symbol.strip().upper()
    if side:
        side_up = side.strip().upper()
        if side_up not in ("BUY", "SELL", "HOLD"):
            raise HTTPException(
                status_code=400,
                detail="side must be BUY, SELL, or HOLD",
            )
        q["side"] = side_up

    rows = await db[EXTERNAL_SIGNALS].find(
        q, _PROJECTION_RECENT,
    ).sort("received_at", -1).to_list(limit)

    # Totals snapshot for the panel header
    totals = {
        "total_24h": await db[EXTERNAL_SIGNALS].count_documents({
            "received_at": {
                "$gte": (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
            },
        }),
        "total_in_window": len(rows),
    }

    # Per-source breakdown so the operator sees which witness is loud
    pipeline = [
        {"$match": q},
        {"$group": {
            "_id": {"source": "$source", "side": "$side"},
            "n": {"$sum": 1},
        }},
        {"$sort": {"n": -1}},
    ]
    by_source: dict[str, dict[str, int]] = {}
    async for d in db[EXTERNAL_SIGNALS].aggregate(pipeline):
        src = d["_id"]["source"]
        sd = d["_id"]["side"]
        by_source.setdefault(src, {})[sd] = d["n"]

    return {
        "items": rows,
        "count": len(rows),
        "window_hours": hours,
        "totals": totals,
        "by_source": by_source,
        # Doctrine banner — surfaced in API too so any consumer is
        # reminded that these are advisory.
        "doctrine": (
            "TRIAL COURT, NOT A VOTING SYSTEM. "
            "External signals are default-hostile; influence_allowed=False "
            "until Verifier promotes the source. Nothing here moves a trade."
        ),
    }


@router.get("/admin/external-signals/credibility")
async def list_source_credibility(_user=Depends(get_current_user)):
    """Return the per-source credibility ledger snapshot.

    This is Verifier's case file for each witness source. Operator-
    readable; Verifier-writable. The panel renders one row per
    source with status, samples, win/loss counters, and the
    rolling verified_alpha.
    """
    rows = await db[EXTERNAL_SOURCE_CREDIBILITY].find(
        {}, {"_id": 0},
    ).sort("source", 1).to_list(None)
    return {
        "items": rows,
        "count": len(rows),
        "doctrine": (
            "Verifier-owned. Webhook may $setOnInsert default-hostile rows; "
            "promotion/demotion is Verifier's job. Phase progression: "
            "UNTRUSTED → WATCHLIST → TRUSTED (and reverse)."
        ),
    }


@router.post("/admin/verifier/resolve-witnesses/{source}")
async def resolve_witnesses(
    source: str,
    horizon_hours: int = Query(default=24, ge=1, le=168),
    dry_run: bool = Query(
        default=True,
        description=(
            "Default True: report what WOULD happen without writing. "
            "Explicitly pass ?dry_run=false to actually run the resolver, "
            "update the ledger, and (if thresholds hit) promote/demote."
        ),
    ),
    _user=Depends(get_current_user),
):
    """Force-run the witness W/L resolver for a given source.

    2026-07-07: The Verifier's witness-outcome resolver was dormant
    since 2026-06-28 (schema shipped, resolver code missing). This
    endpoint is the operator's manual trigger — MVP does not schedule
    the resolver in a background loop yet; the operator runs this
    endpoint on demand and reviews the summary.

    Price fetching:
        Uses `shared_ohlcv_bars` as the canonical price history
        source, honoring the broker-primary priority in
        `shared.research.bar_source.SOURCE_PRIORITY`. Equity symbols
        resolve at 1d timeframe, crypto pairs at 1h. Returns None
        when no bar is on file for the target timestamp — the row
        is then counted as `skipped_price_missing`, not misclassified.
    """
    from verifier.witness_resolver import resolve_source  # noqa: WPS433
    from verifier.price_fetcher import price_from_ohlcv_bars  # noqa: WPS433
    from datetime import datetime, timedelta, timezone

    if dry_run:
        now = datetime.now(timezone.utc)
        cutoff = (now - timedelta(hours=horizon_hours)).isoformat()
        pending = await db[EXTERNAL_SIGNALS].count_documents({
            "source": source,
            "resolution_outcome": {"$exists": False},
            "bar_close_ts": {"$lte": cutoff},
        })
        return {
            "dry_run": True,
            "source": source,
            "horizon_hours": horizon_hours,
            "rows_pending_resolution": pending,
            "note": (
                "Dry run. Pass ?dry_run=false to actually resolve. "
                "Price source: shared_ohlcv_bars (broker-primary via "
                "bar_source.pick_source). Rows with no bar coverage "
                "at the target timestamp will be counted as "
                "skipped_price_missing rather than misclassified."
            ),
        }

    summary = await resolve_source(
        source,
        price_from_ohlcv_bars,
        horizon_hours=horizon_hours,
    )
    return {
        "dry_run": False,
        "source": summary.source,
        "rows_examined": summary.rows_examined,
        "rows_resolved": summary.rows_resolved,
        "rows_undetermined": summary.rows_undetermined,
        "rows_skipped_price_missing": summary.rows_skipped_price_missing,
        "rows_skipped_too_recent": summary.rows_skipped_too_recent,
        "aggregate_before": summary.aggregate_before,
        "aggregate_after": summary.aggregate_after,
        "status_before": summary.status_before,
        "status_after": summary.status_after,
        "status_changed": summary.status_changed,
    }


# ──────────────────────── Seat-bound cleaned context ────────────────────────


@router.get("/admin/external-signals/seat-context")
async def seat_context(
    _user=Depends(get_current_user),
    symbol: Optional[str] = Query(default=None, description="filter by ticker"),
    hours: int = Query(default=24, ge=1, le=72),
    limit: int = Query(default=50, ge=1, le=200),
):
    """Return CLEANED witness context for the Seat-bound view.

    Doctrine pin (TRIAL COURT):
        The witness page displays everything (raw). RoadGuard labels
        suspicious clusters (SPAM, DUPLICATE_BURST, FLIP_FLOP,
        SOFT_NEWS_CLUSTER, SOURCE_DRIFT). This endpoint returns
        ONLY rows that carry NO RoadGuard labels — i.e. witnesses
        that survived the noise filter.

        Even so, the rows returned are STILL `verifier_status=UNTRUSTED`
        and `influence_allowed=False`. The Seat sees them as
        ADVISORY context, not authority. This endpoint does not
        change that. The cleaned set is just "less noise" — not
        "promoted to trusted."

        The response also reports what was filtered out (counts per
        label) so the operator can audit the filter's behavior. If
        SOFT_NEWS_CLUSTER is silently dropping 60% of NVDA witnesses,
        the operator deserves to see that number explicitly, not
        discover it later.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    base_q: dict[str, Any] = {"received_at": {"$gte": cutoff}}
    if symbol:
        base_q["symbol"] = symbol.strip().upper()

    # Cleaned: no roadguard_labels OR explicitly empty
    cleaned_q = {
        **base_q,
        "$or": [
            {"roadguard_labels": {"$exists": False}},
            {"roadguard_labels": {"$size": 0}},
        ],
    }
    cleaned = await db[EXTERNAL_SIGNALS].find(
        cleaned_q, _PROJECTION_RECENT,
    ).sort("received_at", -1).to_list(limit)

    # Filter audit: what got filtered out, by label
    filtered_q = {
        **base_q,
        "roadguard_labels": {"$exists": True, "$not": {"$size": 0}},
    }
    total_filtered = await db[EXTERNAL_SIGNALS].count_documents(filtered_q)
    pipeline = [
        {"$match": filtered_q},
        {"$unwind": "$roadguard_labels"},
        {"$group": {"_id": "$roadguard_labels", "n": {"$sum": 1}}},
        {"$sort": {"n": -1}},
    ]
    filtered_by_label: dict[str, int] = {}
    async for d in db[EXTERNAL_SIGNALS].aggregate(pipeline):
        filtered_by_label[d["_id"]] = d["n"]

    total_in_window = await db[EXTERNAL_SIGNALS].count_documents(base_q)

    return {
        "items": cleaned,
        "count": len(cleaned),
        "window_hours": hours,
        "totals": {
            "total_in_window": total_in_window,
            "cleaned_shown": len(cleaned),
            "filtered_out": total_filtered,
        },
        "filtered_by_label": filtered_by_label,
        "doctrine": (
            "SEAT-BOUND CLEANED CONTEXT. Rows here survived RoadGuard "
            "label filtering. They are still UNTRUSTED and "
            "influence_allowed=False. Read-only advisory context — "
            "the Seat does not act on these, the Seat is INFORMED by these. "
            "Verifier (future) decides if any source ever earns weight."
        ),
    }


# ──────────────────────── Verifier runner + influence status ────────────────────────


@router.get("/admin/verifier/runner-status")
async def verifier_runner_status(_user=Depends(get_current_user)):
    """Runner + influence ceiling status for each configured witness source.

    Two questions this answers in one call:

        1. Is the resolver actually ticking? (`last_run_ts`, `last_run_ok`,
           and `last_summary` from `verifier_runner_state`.)
        2. What CEILING influence would the Governor grant this source
           right now? (`modifier_cap` from `witness_influence`.)

    Sources returned = env-configured resolver targets (default: polygon).
    Read-only. Never mutates the ledger or the runner state.
    """
    import os
    from verifier.witness_resolver_runner import get_runner_state
    from shared.witness_influence import witness_influence_snapshot

    raw = os.environ.get("WITNESS_RESOLVER_SOURCES", "polygon")
    sources = [s.strip() for s in raw.split(",") if s.strip()]

    influence = await witness_influence_snapshot(sources)
    runner_by_source: dict[str, Optional[dict]] = {}
    for src in sources:
        runner_by_source[src] = await get_runner_state(src)

    return {
        "sources": sources,
        "influence": influence,
        "runner": runner_by_source,
        "doctrine": (
            "Runner is a background scheduler that calls the same "
            "resolve_source(...) the admin trigger uses. The ledger "
            "is Verifier-owned; influence.modifier_cap is the CEILING "
            "the Governor may grant when a witness stance is judged "
            "orthogonal — non-orthogonal signals still receive 0.0. "
            "Any tier the ledger doesn't recognize maps to 0.0 by "
            "default-hostile doctrine."
        ),
    }



# ──────────────────────── Verifier calibration harness ────────────────────────


@router.post("/admin/verifier/calibrate/{source}")
async def calibrate_witness(
    source: str,
    mode: str = Query(
        default="threshold",
        description=(
            "'threshold' — offline sweep over already-resolved rows "
            "(fast, milliseconds). "
            "'horizon' — sampled sweep with bar refetch (slow, seconds)."
        ),
    ),
    thresholds_bps: str = Query(
        default="25,50,100,200",
        description="csv of directional thresholds (basis points)",
    ),
    horizons_hours: str = Query(
        default="6,12,24,48,72",
        description="csv of horizons (hours) — used only when mode=horizon",
    ),
    sample_size: int = Query(
        default=500, ge=50, le=2000,
        description="sample size for horizon mode",
    ),
    seed: Optional[int] = Query(
        default=None,
        description="RNG seed for reproducible horizon samples",
    ),
    _user=Depends(get_current_user),
):
    """Read-only calibration sweep — what would this source's win rate
    be at different (horizon, threshold) combinations?

    Threshold sweep is instant (reads stored `resolution_return_bps`);
    horizon sweep costs O(sample × horizons) bar fetches.

    Never mutates `external_source_credibility`. Never rewrites
    `resolution_outcome` on witness rows. The credibility ledger is
    the resolver's job, not the calibrator's. Operator reads the
    returned matrix and decides what env values to lock in.

    Response body includes a `cells` array where each cell is one
    (horizon, threshold) pair with sample counts, win rate, avg
    return, and a `would_promote_to_watchlist` flag using the same
    50-samples / >50%-win-rate rule the resolver enforces.
    """
    from verifier.witness_calibration import (
        calibrate_horizons_sampled,
        calibrate_thresholds_offline,
    )
    from verifier.price_fetcher import price_from_ohlcv_bars

    def _parse_int_csv(raw: str) -> list[int]:
        out: list[int] = []
        for token in raw.split(","):
            token = token.strip()
            if not token:
                continue
            try:
                out.append(int(token))
            except ValueError:
                raise HTTPException(
                    status_code=400,
                    detail=f"bad integer in csv: {token!r}",
                )
        return out

    if mode == "threshold":
        thresholds = _parse_int_csv(thresholds_bps)
        if not thresholds:
            raise HTTPException(
                status_code=400,
                detail="thresholds_bps must be a non-empty csv",
            )
        return await calibrate_thresholds_offline(
            source=source,
            thresholds_bps=thresholds,
        )

    if mode == "horizon":
        horizons = _parse_int_csv(horizons_hours)
        if not horizons:
            raise HTTPException(
                status_code=400,
                detail="horizons_hours must be a non-empty csv",
            )
        return await calibrate_horizons_sampled(
            source=source,
            horizons_hours=horizons,
            price_fetcher=price_from_ohlcv_bars,
            sample_size=sample_size,
            seed=seed,
        )

    raise HTTPException(
        status_code=400,
        detail=f"unknown mode {mode!r}; use 'threshold' or 'horizon'",
    )
