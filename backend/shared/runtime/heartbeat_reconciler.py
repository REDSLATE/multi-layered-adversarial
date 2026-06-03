"""Heartbeat reconciler worker — durability against silent side-effect drift.

Doctrine:
    The sidecar check-in handler ALREADY bumps `shared_heartbeats.last_seen`
    as a side-effect (see `shared/runtime/sidecar_checkin.py`, the
    `# 2026-02-19 — heartbeat side-effect` block). That's the
    canonical happy path.

    But the side-effect is wrapped in `try/except: pass` so a transient
    Mongo write failure silently swallows the heartbeat update while the
    `sidecar_checkin_audit` row above DID persist. After such a glitch,
    the operator sees the imposter scan showing fresh check-ins for a
    brain while the Diagnostics LIVE/STALE/DEAD badge says STALE/DEAD.
    The 2026-06-03 REDEYE pattern — 19 check-ins in last 1h but DEAD
    375s — looks exactly like this failure shape (though the most
    likely actual cause is the REDEYE pod genuinely going silent in
    the last few minutes).

    This worker closes the durability gap. Every tick (default 60s):
      1. For each brain in DISCUSSION_PARTICIPANTS, query the latest
         `sidecar_checkin_audit.ts` row.
      2. Compare against the current `shared_heartbeats.last_seen`.
      3. If the audit row is newer, upsert `shared_heartbeats` with
         `detail.source = "heartbeat_reconciler"` so the operator
         can see the bump came from reconciliation, not a real ping.

    ADVISORY OBSERVABILITY ONLY:
      * Never reassigns a seat
      * Never vetoes an intent
      * Never gates execution
      * Only writes to `shared_heartbeats` (display-only column)

Config (env, with safe defaults):
    HEARTBEAT_RECONCILER_ENABLED   true|false   default: true
    HEARTBEAT_RECONCILER_TICK_SEC  int seconds  default: 60
    HEARTBEAT_RECONCILER_MAX_AGE_S int seconds  default: 1800  (30 min)
        Audit rows older than this won't be reconciled. Stops us from
        accidentally bumping a heartbeat back to "fresh" using a
        check-in that landed 6 hours ago — that's not freshness,
        that's history.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Optional


logger = logging.getLogger("risedual.heartbeat_reconciler")


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        logger.warning(
            "heartbeat_reconciler: bad %s=%r, falling back to %s",
            name, raw, default,
        )
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


RECONCILER_ENABLED_DEFAULT = True
RECONCILER_TICK_SEC_DEFAULT = 60
RECONCILER_MAX_AGE_SEC_DEFAULT = 30 * 60


_worker_task: Optional[asyncio.Task] = None


async def perform_reconcile(max_age_sec: int) -> dict:
    """Single pass of the reconciliation. Returns a summary dict
    suitable for log lines and operator endpoints.

    Pure function-of-DB-state. Safe to call from a route as well as
    the worker loop — gives us a hook for an on-demand
    `POST /api/admin/heartbeat-reconcile/run` later if operators
    want a manual trigger.
    """
    from db import db
    from namespaces import DISCUSSION_PARTICIPANTS, SHARED_HEARTBEATS

    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    bumped: list[dict] = []
    skipped_stale: list[dict] = []
    no_change: list[dict] = []
    no_audit: list[str] = []

    for brain in DISCUSSION_PARTICIPANTS:
        # Latest audit row for this brain — the freshest proof-of-life.
        last_audit = await db["sidecar_checkin_audit"].find_one(
            {"runtime": brain},
            {"_id": 0, "ts": 1, "ts_epoch": 1, "verdict": 1, "source_ip": 1},
            sort=[("ts", -1)],
        )
        if not last_audit:
            no_audit.append(brain)
            continue

        audit_iso = last_audit.get("ts")
        try:
            audit_dt = datetime.fromisoformat(
                (audit_iso or "").replace("Z", "+00:00"),
            )
            audit_age = (now - audit_dt).total_seconds()
        except Exception:  # noqa: BLE001
            # Malformed audit timestamp — skip, audit log is best-effort.
            no_audit.append(brain)
            continue

        # Refuse to reconcile from ancient audit rows. The point of this
        # worker is "the pod IS alive but MC's heartbeat row drifted" —
        # not "rewrite history."
        if audit_age > max_age_sec:
            skipped_stale.append({
                "brain": brain,
                "audit_age_sec": int(audit_age),
                "max_age_sec": max_age_sec,
            })
            continue

        # Current heartbeat (if any). Compare timestamps; bump only if
        # the audit is strictly newer than the heartbeat row.
        hb = await db[SHARED_HEARTBEATS].find_one(
            {"runtime": brain},
            {"_id": 0, "last_seen": 1},
        )
        hb_iso = (hb or {}).get("last_seen")
        try:
            hb_dt = datetime.fromisoformat(
                (hb_iso or "").replace("Z", "+00:00"),
            ) if hb_iso else None
        except Exception:  # noqa: BLE001
            hb_dt = None

        # Already fresh? Skip — avoid noisy writes.
        if hb_dt is not None and hb_dt >= audit_dt:
            no_change.append(brain)
            continue

        # Audit is newer. Upsert.
        await db[SHARED_HEARTBEATS].update_one(
            {"runtime": brain},
            {
                "$set": {
                    "runtime": brain,
                    "status": "ok",
                    "last_seen": audit_iso,
                    "detail": {
                        "source": "heartbeat_reconciler",
                        "via": (
                            "periodic reconcile from sidecar_checkin_audit "
                            "(per-request side-effect missed or stale)"
                        ),
                        "verdict": last_audit.get("verdict"),
                        "source_ip": last_audit.get("source_ip"),
                        "audit_ts": audit_iso,
                        "reconciled_at": now_iso,
                    },
                },
                "$setOnInsert": {"first_seen_at": audit_iso},
                "$inc": {"reconcile_count": 1},
            },
            upsert=True,
        )
        bumped.append({
            "brain": brain,
            "audit_ts": audit_iso,
            "prev_hb_ts": hb_iso,
            "lag_sec": int((audit_dt - hb_dt).total_seconds()) if hb_dt else None,
        })

    return {
        "ts": now_iso,
        "bumped_count": len(bumped),
        "bumped": bumped,
        "no_change_count": len(no_change),
        "no_change": no_change,
        "skipped_stale_count": len(skipped_stale),
        "skipped_stale": skipped_stale,
        "no_audit_count": len(no_audit),
        "no_audit": no_audit,
        "doctrine": "advisory_observability_only",
    }


async def _loop() -> None:
    tick_sec = _env_int(
        "HEARTBEAT_RECONCILER_TICK_SEC", RECONCILER_TICK_SEC_DEFAULT,
    )
    max_age_sec = _env_int(
        "HEARTBEAT_RECONCILER_MAX_AGE_S", RECONCILER_MAX_AGE_SEC_DEFAULT,
    )
    logger.info(
        "heartbeat_reconciler started: tick=%ss max_age=%ss",
        tick_sec, max_age_sec,
    )

    while True:
        try:
            result = await perform_reconcile(max_age_sec=max_age_sec)
            if result["bumped_count"] > 0:
                logger.warning(
                    "heartbeat_reconciler tick: bumped %d brain(s): %s",
                    result["bumped_count"],
                    [
                        f"{b['brain']}(+{b['lag_sec']}s)"
                        for b in result["bumped"]
                    ],
                )
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning("heartbeat_reconciler tick error: %r", e)
        await asyncio.sleep(tick_sec)


def start_worker() -> None:
    """Start the background task. No-op if already running or disabled."""
    global _worker_task
    if not _env_bool(
        "HEARTBEAT_RECONCILER_ENABLED", RECONCILER_ENABLED_DEFAULT,
    ):
        logger.info(
            "heartbeat_reconciler disabled via "
            "HEARTBEAT_RECONCILER_ENABLED=false",
        )
        return
    if _worker_task is not None and not _worker_task.done():
        return
    _worker_task = asyncio.create_task(_loop())


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
