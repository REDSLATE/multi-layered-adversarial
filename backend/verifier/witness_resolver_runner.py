"""Scheduled background runner for the witness W/L resolver.

Doctrine pin (2026-02-19, operator directive):
    The Verifier's witness resolver code was written on 2026-07-07 but
    never scheduled. Phase-0 check on 2026-02-19 counted 4,846
    polygon witness rows in `external_signals` with a credibility
    ledger stuck at `samples=0, wins=0, losses=0, status=UNTRUSTED`.
    Polygon could never earn promotion because the resolver had
    never run against its accumulated rows.

    This runner is the missing scheduled tick. It calls the SAME
    `resolve_source(...)` entrypoint the admin trigger uses, at a
    configurable cadence. No new resolver logic — just a loop.

Mirrors the `opinion_silence_worker` pattern:
    * `_loop()` — infinite tick body
    * `start_worker()` — idempotent boot registration
    * `stop_worker()` — graceful shutdown

Config (env, with safe defaults):
    WITNESS_RESOLVER_ENABLED       true|false     default: true
    WITNESS_RESOLVER_TICK_SEC      int seconds    default: 900   (15 min)
    WITNESS_RESOLVER_SOURCES       csv list       default: "polygon"
    WITNESS_RESOLVER_HORIZON_HOURS int hours      default: 24
    WITNESS_RESOLVER_LIMIT         int rows/pass  default: 5000

Cadence rationale:
    15 min is small enough to see the first promotion within a day
    of the resolver being turned on (polygon at ~4,800 accumulated
    rows will resolve most of them in the first few ticks), and
    large enough not to hammer the broker/polygon bar-history APIs
    the price fetcher hits.

Runner state is persisted to `verifier_runner_state` (one doc per
source) so the operator can see last-tick summary + timing without
tailing logs. Read via `GET /api/admin/verifier/runner-status`.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Optional

from db import db


logger = logging.getLogger("risedual.witness_resolver_runner")


RUNNER_STATE_COLLECTION = "verifier_runner_state"

DEFAULT_ENABLED = True
DEFAULT_TICK_SEC = 15 * 60
DEFAULT_SOURCES = "polygon"
DEFAULT_HORIZON_HOURS = 24
DEFAULT_LIMIT = 5000


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        logger.warning(
            "witness_resolver_runner: bad %s=%r, using %s", name, raw, default,
        )
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_csv(name: str, default: str) -> list[str]:
    raw = os.environ.get(name, default)
    return [s.strip() for s in raw.split(",") if s.strip()]


_worker_task: Optional[asyncio.Task] = None


async def _record_tick_result(source: str, summary_dict: dict, error: Optional[str]) -> None:
    """Persist last-tick summary for operator visibility.

    Written under `_id = f"witness_resolver:{source}"` so the runner-
    status route can look it up without a full scan.
    """
    now = datetime.now(timezone.utc).isoformat()
    await db[RUNNER_STATE_COLLECTION].update_one(
        {"_id": f"witness_resolver:{source}"},
        {
            "$set": {
                "source": source,
                "last_run_ts": now,
                "last_run_ok": error is None,
                "last_error": error,
                "last_summary": summary_dict,
            },
            "$setOnInsert": {"created_at": now},
        },
        upsert=True,
    )


async def _resolve_once(source: str, horizon_hours: int, limit: int) -> None:
    """One resolver pass for one source. Errors are logged + persisted;
    they never propagate — the runner must survive a bad tick."""
    from verifier.witness_resolver import resolve_source
    from verifier.price_fetcher import price_from_ohlcv_bars

    try:
        summary = await resolve_source(
            source,
            price_from_ohlcv_bars,
            horizon_hours=horizon_hours,
            limit=limit,
        )
        summary_dict = {
            "rows_examined": summary.rows_examined,
            "rows_resolved": summary.rows_resolved,
            "rows_undetermined": summary.rows_undetermined,
            "rows_skipped_price_missing": summary.rows_skipped_price_missing,
            "rows_skipped_too_recent": summary.rows_skipped_too_recent,
            "aggregate_after": summary.aggregate_after,
            "status_before": summary.status_before,
            "status_after": summary.status_after,
            "status_changed": summary.status_changed,
        }
        if summary.status_changed:
            logger.warning(
                "witness_resolver_runner: %s %s → %s (samples=%s)",
                source, summary.status_before, summary.status_after,
                summary.aggregate_after.get("samples"),
            )
        else:
            logger.info(
                "witness_resolver_runner tick: %s examined=%s resolved=%s "
                "status=%s samples=%s",
                source, summary.rows_examined, summary.rows_resolved,
                summary.status_after,
                summary.aggregate_after.get("samples"),
            )
        await _record_tick_result(source, summary_dict, error=None)
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        logger.warning("witness_resolver_runner tick %s error: %r", source, e)
        await _record_tick_result(source, summary_dict={}, error=str(e)[:500])


async def _loop() -> None:
    """Main background loop. Idempotent if called more than once."""
    tick_sec = _env_int("WITNESS_RESOLVER_TICK_SEC", DEFAULT_TICK_SEC)
    sources = _env_csv("WITNESS_RESOLVER_SOURCES", DEFAULT_SOURCES)
    horizon_hours = _env_int(
        "WITNESS_RESOLVER_HORIZON_HOURS", DEFAULT_HORIZON_HOURS,
    )
    limit = _env_int("WITNESS_RESOLVER_LIMIT", DEFAULT_LIMIT)
    logger.info(
        "witness_resolver_runner started: tick=%ss sources=%s horizon=%sh limit=%s",
        tick_sec, sources, horizon_hours, limit,
    )
    while True:
        for source in sources:
            await _resolve_once(source, horizon_hours, limit)
        try:
            await asyncio.sleep(tick_sec)
        except asyncio.CancelledError:
            raise


def start_worker() -> None:
    """Start the background task. No-op if already running or disabled."""
    global _worker_task
    if not _env_bool("WITNESS_RESOLVER_ENABLED", DEFAULT_ENABLED):
        logger.info(
            "witness_resolver_runner disabled via "
            "WITNESS_RESOLVER_ENABLED=false",
        )
        return
    if _worker_task is not None and not _worker_task.done():
        return
    _worker_task = asyncio.create_task(_loop(), name="witness_resolver_runner")


async def stop_worker() -> None:
    """Cancel the background task (graceful shutdown)."""
    global _worker_task
    task = _worker_task
    _worker_task = None
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass


async def get_runner_state(source: str) -> Optional[dict]:
    """Read the last-tick record for a source. Used by admin endpoint."""
    return await db[RUNNER_STATE_COLLECTION].find_one(
        {"_id": f"witness_resolver:{source}"}, {"_id": 0},
    )
