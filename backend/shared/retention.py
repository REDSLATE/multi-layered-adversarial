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
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from db import db

logger = logging.getLogger("risedual.retention")

RETENTION_ENABLED = os.environ.get("RETENTION_ENABLED", "true").lower() == "true"
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "3"))
RETENTION_SWEEP_INTERVAL_SEC = int(os.environ.get("RETENTION_SWEEP_INTERVAL_SEC", "3600"))
RETENTION_BATCH_SIZE = int(os.environ.get("RETENTION_BATCH_SIZE", "2000"))
RETENTION_MAX_BATCHES = int(os.environ.get("RETENTION_MAX_BATCHES", "50"))
_BOOT_DELAY_SEC = 60.0
_BATCH_SLEEP_SEC = 0.2

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


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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


async def _loop() -> None:
    await asyncio.sleep(_BOOT_DELAY_SEC)
    logger.info(
        "retention sweeper started: days=%s interval=%ss batch=%s×%s",
        RETENTION_DAYS, RETENTION_SWEEP_INTERVAL_SEC,
        RETENTION_BATCH_SIZE, RETENTION_MAX_BATCHES,
    )
    while True:
        try:
            await run_cycle()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("retention loop error: %s", exc)
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
