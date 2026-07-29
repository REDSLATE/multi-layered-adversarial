"""Retention sweeper — 72-hour expiry for pipeline/telemetry backlog.

Operator directive (2026-07-22): "eliminate the backlog, expire after
7 days. The only data we need is the actual executions. The ones that
failed we can eliminate." Tightened to 72 hours (3 days) on operator
request to further relieve Atlas IOPS.

Keeps FOREVER:
  * `shared_intents` rows with `executed=true`  (real trades)
  * `executions` rows with `ok=true`            (broker-accepted)
  * `shared_broker_fills`                       (canonical fills)
  * config/credentials/controls/flags/universe/capital ledger

Everything else in RULES expires RETENTION_DAYS (default 7) after its
timestamp. Deletes run in bounded batches (id-list + delete_many $in)
with sleeps between batches so a saturated Atlas tier is drained
progressively instead of hammered. Direct motivation: prod Atlas is
failing reads with "operation exceeded time limit" under the weight
of millions of stale telemetry rows.

Env tunables:
  RETENTION_ENABLED=true            master gate
  RETENTION_DAYS=3                  age threshold (72h)
  RETENTION_SWEEP_INTERVAL_SEC=3600 cycle cadence
  RETENTION_BATCH_SIZE=2000         ids per delete batch
  RETENTION_MAX_BATCHES=50          per-collection cap per cycle

Retention-health monitor (2026-07-29)
-------------------------------------
`mc_brain_silences` accumulated 334k rows under a TTL index keyed on
a STRING field (Mongo's reaper silently ignores non-Date keys), and
nobody noticed for months. The sampler below periodically records
`estimated_document_count()` per swept collection into
`retention_health_snapshots`, and `evaluate_retention_health()`
diffs the latest counts against a trailing baseline so a collection
growing past `baseline * factor` becomes a visible warn/fail in
`/api/admin/healthcheck/full`.

All sampler DB work runs on `worker_db` (the capped-pool client) and
uses `estimated_document_count` — O(1) collection metadata, never a
scan — so the monitor can never itself saturate Atlas or starve the
request-serving pool.

  RETENTION_HEALTH_INTERVAL_SEC=604800   sampler cadence (7 days)
  RETENTION_HEALTH_GROWTH_FACTOR=2.0     count/baseline warn ratio
  RETENTION_HEALTH_MIN_COUNT=5000        absolute floor (noise gate)
  RETENTION_HEALTH_FAIL_FACTOR=4.0       ratio escalating warn->fail
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from db import db, worker_db

logger = logging.getLogger("risedual.retention")

RETENTION_ENABLED = os.environ.get("RETENTION_ENABLED", "true").lower() == "true"
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "3"))
RETENTION_SWEEP_INTERVAL_SEC = int(os.environ.get("RETENTION_SWEEP_INTERVAL_SEC", "3600"))
RETENTION_BATCH_SIZE = int(os.environ.get("RETENTION_BATCH_SIZE", "2000"))
RETENTION_MAX_BATCHES = int(os.environ.get("RETENTION_MAX_BATCHES", "50"))
_BOOT_DELAY_SEC = 60.0
_BATCH_SLEEP_SEC = 0.2

HEALTH_COLLECTION = "retention_health_snapshots"
HEALTH_SNAPSHOT_TTL_DAYS = 90
_HEALTH_COUNT_TIMEOUT_S = 5.0

# (collection, ts_field, is_bson_date, extra_filter)
RULES: list[tuple[str, str, bool, Optional[dict]]] = [
    ("mc_shelly", "ts", False, None),
    ("shared_ohlcv_bars", "ts", False, None),
    ("mc_brain_silences", "at", False, None),
    ("shared_gate_results", "ts", False, None),
    ("shared_brain_conflicts", "detected_at", False, None),
    ("runtime_token_rejections", "ts", False, None),
    ("risk_monitor_evaluations", "ts", False, None),
    ("mc_opinions_compare", "ts", False, None),
    ("mc_seats", "ts", False, None),
    ("doctrine_sidecars", "ts", False, None),
    ("shared_governance_decisions", "ts", False, None),
    ("paradox_records", "created_at", True, None),
    ("sovereign_audit_log", "ts", False, None),
    ("mc_parity_manifests", "recorded_at", False, None),
    ("paradox_v2_brain_votes", "timestamp", False, None),
    ("sovereign_state_history", "ts", False, None),
    ("public_request_log", "ts", False, None),
    ("shared_adl_receipts", "timestamp", False, None),
    ("mc_pulses", "started_at", False, None),
    ("sidecar_checkin_audit", "ts", False, None),
    ("sovereign_contribution_attempts", "ts", False, None),
    ("external_signals", "received_at", False, None),
    ("observation_receipts", "created_at", False, None),
    ("shared_vrl_scorecards", "window_end", False, None),
    # Intents: executed ones are the trade record — kept forever.
    ("shared_intents", "ingest_ts", False, {"executed": {"$ne": True}}),
    # Executions: broker-ACCEPTED rows kept forever; failed attempts expire.
    ("executions", "ts", False, {"ok": {"$ne": True}}),
]

_TASK: Optional[asyncio.Task] = None
_RUNNING: bool = False
_LAST_RUN_AT: Optional[str] = None
_LAST_RUN_SEC: Optional[float] = None
_LAST_CYCLE: dict[str, Any] = {}
_TOTAL_DELETED: int = 0
_CYCLE_COUNT: int = 0
_LAST_ERROR: Optional[str] = None

_LAST_HEALTH_SAMPLE_AT: Optional[str] = None
_LAST_HEALTH_SAMPLE: dict[str, Any] = {}
_HEALTH_SAMPLE_COUNT: int = 0
_LAST_HEALTH_ERROR: Optional[str] = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def health_interval_sec() -> int:
    """Sampler cadence. Default 7 days — one signal per week is
    plenty for a failure mode that took months to notice."""
    return _env_int("RETENTION_HEALTH_INTERVAL_SEC", 604800)


async def _purge_collection(
    coll: str, field: str, is_date: bool, extra: Optional[dict],
) -> dict:
    """Delete expired docs in bounded batches. Returns per-coll stats."""
    cutoff_dt = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
    cutoff: Any = cutoff_dt if is_date else cutoff_dt.isoformat()
    q: dict = {field: {"$lt": cutoff}}
    if extra:
        q.update(extra)
    deleted = 0
    batches = 0
    capped = False
    while batches < RETENTION_MAX_BATCHES:
        ids = [
            d["_id"]
            for d in await db[coll]
            .find(q, {"_id": 1})
            .limit(RETENTION_BATCH_SIZE)
            .max_time_ms(20000)
            .to_list(RETENTION_BATCH_SIZE)
        ]
        if not ids:
            break
        res = await db[coll].delete_many({"_id": {"$in": ids}})
        deleted += res.deleted_count
        batches += 1
        if len(ids) < RETENTION_BATCH_SIZE:
            break
        await asyncio.sleep(_BATCH_SLEEP_SEC)
    else:
        capped = True  # more work remains — next cycle continues the drain
    return {"deleted": deleted, "batches": batches, "capped": capped}


async def run_cycle() -> dict:
    """One full retention pass across all RULES. Re-entrancy guarded."""
    global _RUNNING, _LAST_RUN_AT, _LAST_RUN_SEC, _LAST_CYCLE
    global _TOTAL_DELETED, _CYCLE_COUNT, _LAST_ERROR
    if _RUNNING:
        return {"ok": False, "error": "cycle already running"}
    _RUNNING = True
    started = asyncio.get_event_loop().time()
    stats: dict[str, Any] = {}
    cycle_deleted = 0
    try:
        for coll, field, is_date, extra in RULES:
            try:
                s = await _purge_collection(coll, field, is_date, extra)
                if s["deleted"]:
                    stats[coll] = s
                    cycle_deleted += s["deleted"]
            except Exception as exc:  # noqa: BLE001
                stats[coll] = {"error": f"{type(exc).__name__}: {exc}"[:150]}
                logger.warning("retention: purge %s failed: %s", coll, exc)
        _LAST_ERROR = None
    except Exception as exc:  # noqa: BLE001
        _LAST_ERROR = f"{type(exc).__name__}: {exc}"[:200]
        logger.exception("retention cycle failed: %s", exc)
    finally:
        _RUNNING = False
        _LAST_RUN_AT = _now_iso()
        _LAST_RUN_SEC = round(asyncio.get_event_loop().time() - started, 2)
        _LAST_CYCLE = stats
        _TOTAL_DELETED += cycle_deleted
        _CYCLE_COUNT += 1
    if cycle_deleted:
        logger.info(
            "retention cycle #%s: deleted %s docs in %.1fs (%s)",
            _CYCLE_COUNT, cycle_deleted, _LAST_RUN_SEC,
            ", ".join(f"{k}:{v.get('deleted')}" for k, v in stats.items() if v.get("deleted")),
        )
    return {
        "ok": _LAST_ERROR is None,
        "deleted": cycle_deleted,
        "took_sec": _LAST_RUN_SEC,
        "collections": stats,
        "error": _LAST_ERROR,
    }


# ── Retention-health monitor ──────────────────────────────────────

_STATUS_RANK = {"pass": 0, "warn": 1, "fail": 2}
# Stay under `WORKER_MAX_POOL_SIZE` so the sampler never queues on
# its own pool while still finishing well inside a healthcheck budget.
_HEALTH_CONCURRENCY = 4


async def _estimated_count(coll: str) -> tuple[Optional[int], Optional[str]]:
    """O(1) count from collection metadata.

    `estimated_document_count` reads the collection's cached document
    count and NEVER scans, unlike `count_documents` which runs a real
    (possibly index-less) count query. On a saturated Atlas tier that
    difference is the whole ballgame: a `count_documents` sweep over
    27 collections is exactly the kind of load this monitor exists to
    detect, not to cause. Per-call timeout so one slow collection
    can't hang the sampler.
    """
    timeout_ms = int(_HEALTH_COUNT_TIMEOUT_S * 1000)
    try:
        n = await asyncio.wait_for(
            worker_db[coll].estimated_document_count(maxTimeMS=timeout_ms),
            timeout=_HEALTH_COUNT_TIMEOUT_S + 1.0,
        )
        return int(n), None
    except asyncio.CancelledError:
        raise
    except asyncio.TimeoutError:
        return None, f"timeout after {_HEALTH_COUNT_TIMEOUT_S}s"
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}"[:120]


async def sample_retention_counts() -> dict:
    """Record one estimated-count snapshot per swept collection.

    Read-only against the swept collections; the only write is one
    doc per collection into `retention_health_snapshots`. Runs on the
    capped-pool `worker_db` client so it can never starve login.
    """
    global _LAST_HEALTH_SAMPLE_AT, _LAST_HEALTH_SAMPLE
    global _HEALTH_SAMPLE_COUNT, _LAST_HEALTH_ERROR
    started = asyncio.get_event_loop().time()
    now = datetime.now(timezone.utc)
    counts, errors = await _estimated_counts_for_rules()
    docs: list[dict] = []
    for coll, count in counts.items():
        if count is None:
            continue
        docs.append({
            "collection": coll,
            "count": count,
            # BSON Dates, not isoformat strings: `ttl_at` is what the
            # TTL index in db.py keys on, and Mongo's reaper silently
            # ignores string-typed fields (the 334k-row bug).
            "ts": now,
            "ttl_at": now,
            "ts_iso": now.isoformat(),
        })
    written = 0
    if docs:
        try:
            res = await asyncio.wait_for(
                worker_db[HEALTH_COLLECTION].insert_many(docs, ordered=False),
                timeout=_HEALTH_COUNT_TIMEOUT_S + 5.0,
            )
            written = len(res.inserted_ids)
            _LAST_HEALTH_ERROR = None
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            _LAST_HEALTH_ERROR = f"{type(exc).__name__}: {exc}"[:200]
            logger.warning("retention health: snapshot write failed: %s", exc)
    took_sec = round(asyncio.get_event_loop().time() - started, 2)
    _LAST_HEALTH_SAMPLE_AT = now.isoformat()
    _LAST_HEALTH_SAMPLE = {k: v for k, v in counts.items() if v is not None}
    _HEALTH_SAMPLE_COUNT += 1
    logger.info(
        "retention health sample #%s: %s/%s collections counted, "
        "%s snapshots written in %.1fs",
        _HEALTH_SAMPLE_COUNT, len(docs), len(RULES), written, took_sec,
    )
    return {
        "ok": _LAST_HEALTH_ERROR is None and not errors,
        "sampled_at": _LAST_HEALTH_SAMPLE_AT,
        "collections_sampled": len(docs),
        "snapshots_written": written,
        "counts": counts,
        "errors": errors,
        "took_sec": took_sec,
        "write_error": _LAST_HEALTH_ERROR,
    }


async def _estimated_counts_for_rules() -> tuple[
    dict[str, Optional[int]], dict[str, str],
]:
    """Estimated count for every collection in `RULES`, a few at a
    time. Returns `(counts, errors)`; a collection that errored has
    `None` for its count and an entry in `errors`."""
    sem = asyncio.Semaphore(_HEALTH_CONCURRENCY)

    async def one(coll: str) -> tuple[str, Optional[int], Optional[str]]:
        async with sem:
            count, err = await _estimated_count(coll)
        return coll, count, err

    results = await asyncio.gather(
        *(one(coll) for coll, _f, _d, _e in RULES),
    )
    counts: dict[str, Optional[int]] = {}
    errors: dict[str, str] = {}
    for coll, count, err in results:
        counts[coll] = count
        if err:
            errors[coll] = err
    return counts, errors


async def _baselines(cutoff: datetime) -> dict[str, int]:
    """Latest snapshot count per collection older than `cutoff` — the
    previous sampling generation, not one just written. Single indexed
    aggregation (the snapshot collection holds ~len(RULES) docs per
    sample, TTL-capped) so the whole evaluation stays one round trip."""
    try:
        rows = await asyncio.wait_for(
            worker_db[HEALTH_COLLECTION].aggregate(
                [
                    {"$match": {"ts": {"$lt": cutoff}}},
                    {"$sort": {"ts": -1}},
                    {"$group": {
                        "_id": "$collection",
                        "count": {"$first": "$count"},
                    }},
                ],
                maxTimeMS=int(_HEALTH_COUNT_TIMEOUT_S * 1000),
            ).to_list(length=500),
            timeout=_HEALTH_COUNT_TIMEOUT_S + 1.0,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("retention health: baseline lookup failed: %s", exc)
        return {}
    return {
        r["_id"]: int(r["count"])
        for r in rows
        if isinstance(r.get("count"), (int, float)) and r.get("_id")
    }


def classify_growth(
    count: Optional[int],
    baseline: Optional[int],
    *,
    growth_factor: float,
    fail_factor: float,
    min_count: int,
) -> tuple[str, Optional[float], str]:
    """(status, growth_ratio, detail) for one collection.

    A collection only trips the alarm when it is BOTH above the
    absolute floor and above `baseline * growth_factor`; small
    collections wobble by large ratios on pure noise.
    """
    if count is None:
        return "warn", None, "count unavailable"
    if baseline is None:
        return "pass", None, f"count={count}, no baseline yet"
    if baseline <= 0:
        return "pass", None, f"count={count}, baseline=0 (nothing to compare)"
    ratio = round(count / baseline, 3)
    if count < min_count:
        return "pass", ratio, (
            f"count={count} below floor {min_count} (ratio={ratio}, ignored)"
        )
    if ratio >= fail_factor:
        return "fail", ratio, (
            f"count={count} is {ratio}× baseline {baseline} — retention "
            f"is NOT keeping up (check the TTL/sweep for this collection)"
        )
    if ratio >= growth_factor:
        return "warn", ratio, (
            f"count={count} is {ratio}× baseline {baseline} — growing "
            f"faster than retention is reclaiming"
        )
    return "pass", ratio, f"count={count}, baseline={baseline}, ratio={ratio}"


async def evaluate_retention_health() -> dict:
    """Fresh estimated counts vs. the trailing baseline snapshot.

    Read-only and bounded: one O(1) `estimated_document_count` per
    rule (a few at a time, each timeout-capped) plus one aggregation
    for all baselines, on the capped-pool worker client. Every
    collection in `RULES` appears in `collections` so the report can
    never silently omit the one that is piling up.
    """
    growth_factor = _env_float("RETENTION_HEALTH_GROWTH_FACTOR", 2.0)
    fail_factor = _env_float("RETENTION_HEALTH_FAIL_FACTOR", 4.0)
    min_count = _env_int("RETENTION_HEALTH_MIN_COUNT", 5000)
    # Ignore snapshots younger than half a sampling interval so a
    # sample written moments ago can't become its own baseline.
    baseline_min_age_sec = max(60.0, health_interval_sec() / 2.0)
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=baseline_min_age_sec)

    baselines = await _baselines(cutoff)
    counts, errors = await _estimated_counts_for_rules()
    rows: list[dict] = []
    for coll, _field, _is_date, _extra in RULES:
        count = counts.get(coll)
        err = errors.get(coll)
        baseline = baselines.get(coll)
        status, ratio, detail = classify_growth(
            count, baseline,
            growth_factor=growth_factor,
            fail_factor=fail_factor,
            min_count=min_count,
        )
        rows.append({
            "collection": coll,
            "count": count,
            "baseline": baseline,
            "growth_ratio": ratio,
            "status": status,
            "detail": f"{detail}; {err}" if err else detail,
        })

    worst = max(
        (_STATUS_RANK.get(r["status"], 1) for r in rows), default=0,
    )
    return {
        "overall": {0: "pass", 1: "warn", 2: "fail"}[worst],
        "evaluated_at": _now_iso(),
        "growth_factor": growth_factor,
        "fail_factor": fail_factor,
        "min_count": min_count,
        "baseline_min_age_sec": baseline_min_age_sec,
        "collections": rows,
        "offenders": [
            r["collection"] for r in rows if r["status"] != "pass"
        ],
        "last_sampled_at": _LAST_HEALTH_SAMPLE_AT,
    }


def get_health_status() -> dict:
    """Sampler introspection — no DB access."""
    return {
        "interval_sec": health_interval_sec(),
        "collection": HEALTH_COLLECTION,
        "snapshot_ttl_days": HEALTH_SNAPSHOT_TTL_DAYS,
        "sample_count": _HEALTH_SAMPLE_COUNT,
        "last_sampled_at": _LAST_HEALTH_SAMPLE_AT,
        "last_counts": dict(_LAST_HEALTH_SAMPLE),
        "last_error": _LAST_HEALTH_ERROR,
    }


async def _loop() -> None:
    await asyncio.sleep(_BOOT_DELAY_SEC)
    logger.info(
        "retention sweeper started: days=%s interval=%ss batch=%s×%s",
        RETENTION_DAYS, RETENTION_SWEEP_INTERVAL_SEC,
        RETENTION_BATCH_SIZE, RETENTION_MAX_BATCHES,
    )
    # The health sampler rides THIS task rather than a second
    # scheduler: one background loop, one place to cancel, and the
    # sampler can never overlap a sweep. First sample fires on the
    # first cycle so the baseline starts accumulating immediately.
    next_health_at = 0.0
    while True:
        try:
            await run_cycle()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("retention loop error: %s", exc)
        now = asyncio.get_event_loop().time()
        if now >= next_health_at:
            next_health_at = now + health_interval_sec()
            try:
                await sample_retention_counts()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.exception("retention health sampler error: %s", exc)
        await asyncio.sleep(RETENTION_SWEEP_INTERVAL_SEC)


def get_status() -> dict:
    return {
        "enabled": RETENTION_ENABLED,
        "retention_days": RETENTION_DAYS,
        "interval_sec": RETENTION_SWEEP_INTERVAL_SEC,
        "task_alive": bool(_TASK is not None and not _TASK.done()),
        "running_now": _RUNNING,
        "cycle_count": _CYCLE_COUNT,
        "last_run_at": _LAST_RUN_AT,
        "last_run_sec": _LAST_RUN_SEC,
        "last_cycle": _LAST_CYCLE,
        "total_deleted": _TOTAL_DELETED,
        "last_error": _LAST_ERROR,
        "kept_forever": [
            "shared_intents (executed=true)", "executions (ok=true)",
            "shared_broker_fills", "config/credentials/controls",
        ],
        "health": get_health_status(),
        "rules": [
            {"collection": c, "field": f, "extra": e} for c, f, _, e in RULES
        ],
    }


def start_worker_if_enabled() -> None:
    global _TASK
    if not RETENTION_ENABLED:
        logger.info("retention sweeper disabled (RETENTION_ENABLED=false)")
        return
    if _TASK and not _TASK.done():
        return
    _TASK = asyncio.get_event_loop().create_task(_loop())


async def stop_worker() -> None:
    global _TASK
    if _TASK and not _TASK.done():
        _TASK.cancel()
        try:
            await _TASK
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    _TASK = None
