"""Feature-coverage report — operational health for doctrine inputs.

Doctrine pin (2026-02-19, operator directive):
    Coverage answers "is this field populated" — the necessary
    precondition for the doctrine seats to consume it. Prevents the
    Part-1 style failure where fields silently defaulted to 0.0 and
    the whole doctrine collapsed into identical scores.

    Reports two scopes:
      * live_universe  — symbols the brains actually emitted intents
                          on in the last N hours. What matters day-
                          to-day. Small, tight, actionable.
      * all_snapshots  — every symbol with a snapshot in the cache.
                          Larger set, includes synthetic test rows
                          and inactive tickers. Surfaces structural
                          gaps at the collection level.

    Per-source health tracks WHY a coverage gap exists — a field
    can be populated today but on-course to break because its
    upstream feeder has been failing for six hours. Read-only
    cross-reference of `feeder_health_audit`.

Doctrine anti-patterns this module WILL NOT do:
    * Mutate anything — pure aggregation, no writes.
    * Auto-remediate low coverage — surfaces the number, the
      operator decides.
    * Report on fields it doesn't know about — the canonical field
      list below is the contract. New fields require an explicit
      addition here (which is doctrine, not incident response).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from db import db
from namespaces import (
    FEEDER_HEALTH_AUDIT,
    SHARED_INDICATOR_SNAPSHOTS,
    SHARED_INTENTS,
)


# ─────────────────────────── Field taxonomy ───────────────────────────
# The doctrine-relevant field list. Grouped so the operator can see at
# a glance which category is healthy vs starved.


SNAPSHOT_FIELD_GROUPS: dict[str, list[str]] = {
    "session_features_v1": [
        "gap_pct",
        "relative_volume",
        "vwap_distance_pct",
    ],
    "session_features_v2": [
        # 2026-02-20 shipped: dual-path volume gate inputs +
        # follow-up A regime/velocity signals.
        # `trend_score` computed intraday from recent-bar slope.
        # `rvol_acceleration` = delta-vs-baseline over last 5 bars.
        # `velocity_5m` = second-derivative curvature (last 3 bars).
        # `market_regime` = SPY-based shared classifier (TTL-cached).
        "rvol_acceleration",
        "trend_score",
        "velocity_5m",
        "market_regime",
    ],
    "microstructure": [
        "spread_bps",
    ],
    "legacy_indicators": [
        # Baseline sanity — should be ~100% for any symbol with bars.
        # Deviation from 100 here indicates a bar-pipeline break, not
        # a doctrine-layer bug.
        "last_close",
        "atr14",
        "rsi14",
    ],
}

ALL_SNAPSHOT_FIELDS: list[str] = [
    f for group in SNAPSHOT_FIELD_GROUPS.values() for f in group
]

# Sources tracked in per_source_health. Missing entries in
# feeder_health_audit surface as "no_data" rather than being silently
# dropped from the report.
TRACKED_SOURCES: list[str] = [
    "polygon_flatfiles",   # daily equity bars
    "polygon_equity",      # legacy REST feeder (disabled)
    "finnhub_equity",      # equity intraday
    "polygon_news_witness",  # witness rows
    "kraken_pro",          # crypto bars
]


# ─────────────────────────── Data structures ───────────────────────────


@dataclass
class FieldCoverage:
    field: str
    populated: int
    total: int
    pct: float
    missing_symbols: list[str]  # capped to first N (see MISSING_SAMPLE_CAP)


@dataclass
class SourceHealth:
    source: str
    last_success_ts: Optional[str]
    last_error_ts: Optional[str]
    last_error_message: Optional[str]
    minutes_since_last_success: Optional[float]


MISSING_SAMPLE_CAP = 20   # cap missing-symbol samples per field
LIVE_UNIVERSE_LOOKBACK_HOURS = 24


# ─────────────────────────── Universe resolution ───────────────────────────


async def _live_universe_symbols(hours: int = LIVE_UNIVERSE_LOOKBACK_HOURS) -> list[str]:
    """Symbols the brains have emitted intents on in the last `hours`.
    Small, actively-traded set — what the operator cares about."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    return await db[SHARED_INTENTS].distinct(
        "symbol", {"ingest_ts": {"$gte": cutoff}},
    )


async def _all_snapshot_symbols() -> list[str]:
    """Every symbol with a snapshot in the cache. Larger set,
    includes inactive / synthetic test rows. Surfaces structural
    coverage regardless of trading activity."""
    return await db[SHARED_INDICATOR_SNAPSHOTS].distinct("symbol")


# ─────────────────────────── Coverage computation ───────────────────────────


def _latest_snapshot_by_symbol(docs: list[dict]) -> dict[str, dict]:
    """From a list of raw snapshot docs, return one-per-symbol keeping
    the most-recently-computed. Handles the case where multiple
    (source, tf) pairs exist per symbol — we pick whichever was
    computed last; the doctrine consumers do the same via the
    `_latest_indicator_snapshot` helper."""
    latest_by_sym: dict[str, dict] = {}
    for d in docs:
        sym = d.get("symbol")
        if not sym:
            continue
        prev = latest_by_sym.get(sym)
        if prev is None or (
            (d.get("computed_at") or "") > (prev.get("computed_at") or "")
        ):
            latest_by_sym[sym] = d
    return latest_by_sym


async def _coverage_for_scope(
    symbols: list[str],
) -> tuple[list[FieldCoverage], int]:
    """Compute per-field coverage for the given symbol set.

    Returns (coverage_list, snapshot_count_resolved). The count differs
    from `len(symbols)` when some symbols have no snapshot at all —
    that's real "no data" and is reflected in each field's populated
    count.
    """
    if not symbols:
        return [], 0

    # One collection query, then pivot in memory. Cheaper than N queries
    # per symbol × M queries per field.
    docs = await db[SHARED_INDICATOR_SNAPSHOTS].find(
        {"symbol": {"$in": symbols}},
        {
            "_id": 0,
            "symbol": 1,
            "computed_at": 1,
            "indicators": 1,
        },
    ).to_list(length=None)
    latest = _latest_snapshot_by_symbol(docs)

    coverage: list[FieldCoverage] = []
    for field in ALL_SNAPSHOT_FIELDS:
        populated = 0
        missing: list[str] = []
        for sym in symbols:
            snap = latest.get(sym)
            val = None
            if snap:
                val = (snap.get("indicators") or {}).get(field)
            if val is None or (isinstance(val, float) and val != val):
                # None or NaN → missing
                if len(missing) < MISSING_SAMPLE_CAP:
                    missing.append(sym)
            else:
                populated += 1
        total = len(symbols)
        pct = round(populated / total * 100.0, 1) if total else 0.0
        coverage.append(FieldCoverage(
            field=field,
            populated=populated,
            total=total,
            pct=pct,
            missing_symbols=missing,
        ))
    return coverage, len(latest)


# ─────────────────────────── Source health ───────────────────────────


async def _source_health() -> list[SourceHealth]:
    """Last-success / last-error snapshot per tracked provider.

    Reads `feeder_health_audit` — one row per provider tick. We fetch
    the latest OK row and the latest error row per provider and
    compute the time-since-last-success. Missing providers surface
    as `no_data` rather than being dropped from the report.
    """
    out: list[SourceHealth] = []
    now = datetime.now(timezone.utc)
    for src in TRACKED_SOURCES:
        # Latest successful (status_code=200 or error_type=None) tick.
        ok_doc = await db[FEEDER_HEALTH_AUDIT].find_one(
            {
                "provider": src,
                "$or": [
                    {"status_code": 200},
                    {"error_type": None},
                ],
            },
            sort=[("ts", -1)],
        )
        # Latest error tick, regardless of type.
        err_doc = await db[FEEDER_HEALTH_AUDIT].find_one(
            {
                "provider": src,
                "error_type": {"$ne": None},
            },
            sort=[("ts", -1)],
        )
        ok_ts = ok_doc.get("ts") if ok_doc else None
        err_ts = err_doc.get("ts") if err_doc else None
        err_msg = (err_doc.get("message") if err_doc else None) or None

        minutes_since = None
        if ok_ts:
            try:
                parsed = datetime.fromisoformat(
                    str(ok_ts).replace("Z", "+00:00"),
                )
                minutes_since = round((now - parsed).total_seconds() / 60.0, 1)
            except (TypeError, ValueError):
                minutes_since = None
        out.append(SourceHealth(
            source=src,
            last_success_ts=ok_ts,
            last_error_ts=err_ts,
            last_error_message=err_msg[:300] if err_msg else None,
            minutes_since_last_success=minutes_since,
        ))
    return out


# ─────────────────────────── Report builder ───────────────────────────


def _serialize_coverage(items: list[FieldCoverage]) -> dict:
    """Group by field-category for the report. Grouping keeps the JSON
    tight and lets the frontend render categories separately without
    having to redo the grouping."""
    field_to_group: dict[str, str] = {}
    for group, fields in SNAPSHOT_FIELD_GROUPS.items():
        for f in fields:
            field_to_group[f] = group

    grouped: dict[str, dict] = {g: {} for g in SNAPSHOT_FIELD_GROUPS}
    for c in items:
        g = field_to_group.get(c.field, "other")
        grouped.setdefault(g, {})[c.field] = {
            "populated": c.populated,
            "total": c.total,
            "pct": c.pct,
            "missing_symbols": c.missing_symbols,
        }
    return grouped


async def build_coverage_report(scope: str) -> dict:
    """Public entry point.

    scope:
      * 'live_universe' — actively-traded symbols (last 24h intents)
      * 'all_snapshots' — full snapshot cache
    """
    if scope == "live_universe":
        symbols = await _live_universe_symbols()
    elif scope == "all_snapshots":
        symbols = await _all_snapshot_symbols()
    else:
        raise ValueError(f"unknown scope: {scope!r}")

    coverage_items, snapshots_resolved = await _coverage_for_scope(symbols)
    coverage = _serialize_coverage(coverage_items)
    health = await _source_health()

    return {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "scope": scope,
        "universe_size": len(symbols),
        "snapshots_resolved": snapshots_resolved,
        "coverage": coverage,
        "per_source_health": [
            {
                "source": h.source,
                "last_success_ts": h.last_success_ts,
                "last_error_ts": h.last_error_ts,
                "last_error_message": h.last_error_message,
                "minutes_since_last_success": h.minutes_since_last_success,
                "status": (
                    "no_data" if h.last_success_ts is None
                    # 2026-02-20 tuning (operator directive): polygon
                    # flatfiles poll every 60min; the previous 120min
                    # stale threshold gave a false-alarm race window
                    # (poll in-flight but not yet completed). Raised
                    # to 180min = 3× poll interval so a single missed
                    # poll doesn't flip green→stale.
                    else "ok" if (h.minutes_since_last_success or 0) < 180
                    else "stale"
                ),
            }
            for h in health
        ],
        "doctrine": (
            "Coverage = fraction of scope symbols where the field is "
            "populated (non-None, non-NaN) on the most-recent snapshot. "
            "per_source_health surfaces feeder-tick liveness — a healthy "
            "coverage number today can still degrade tomorrow if its "
            "upstream feeder has stalled. Read-only; never mutates."
        ),
    }
