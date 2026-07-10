"""Stale-intent sweeper — archive-then-delete cold intents from the
hot `shared_intents` collection.

Operator doctrine (2026-02-19, RISEDUAL):

    Long-term memory should NOT be millions of raw intents. It
    should be the distilled chain:

        Market → Intent → Experience → Outcome → Bucket → Lesson → Doctrine

    Each stage compresses. Once an intent has contributed to a
    resolved `learning_experience`, the original raw row has served
    its purpose — the learning tape is now the memory.

Rule set:

  1. AGE GATE — `ingest_ts < now - 6h`. Anything younger stays;
     the reconcile sweep and learning outcome resolver both work
     within a 60-min horizon so we give a generous cushion.

  2. NEVER-REACHED-BROKER FILTER (all must hold at query time):
       * `executed != true`
       * `broker_order_id` is missing/null/empty
       * `gate_state != "submitted"`

  3. NEVER PURGE — safety carve-outs enforced per-row after query:
       * Anything with an active capital-ledger reservation
         (`capital_ledger.reservations[].status == "open"` for the
         intent_id). The reconciler could still release these.
       * If learning is enabled AND capture is incomplete
         (no `learning_experiences` row for the intent_id), the
         learning loop may still be catching up — preserve.

  4. LEARNING-AWARE BIFURCATION — for each surviving candidate:
       * If a resolved `learning_experience` exists (at least one
         of `outcome_5m_bps`, `outcome_15m_bps`, `outcome_1h_bps`
         is set), the intent's information has been distilled into
         the learning tape. DELETE outright — no archive needed.
         Reason: `distilled_via_learning_experience`.
       * Otherwise, ARCHIVE the row to `shared_intents_archive`
         with the doctrine-mandated stamps, verify the write
         actually landed, THEN delete from the hot collection.
         Reason: `stale_never_reached_broker`.

  5. BATCH BOUNDED — default 500, capped 1000. Prevents a single
     invocation from hammering Atlas with a massive delete.

  6. DRY RUN — the admin endpoint defaults to `dry_run=true`.
     Prints what would be affected without touching Mongo.

  7. SCHEDULER ON BY DEFAULT — 30-minute background loop runs
     from boot. The endpoint remains available for manual
     dry-runs at any time. Flip `INTENT_SWEEPER_ENABLED=false`
     in `backend/.env` to pause the scheduler.

Archive doc shape:

    {
      **original_intent_fields,
      "archived_at": <iso>,
      "archive_reason": "stale_never_reached_broker",
      "original_gate_state": <copy of gate_state at archive time>,
      "archive_version": "v1"
    }
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

logger = logging.getLogger("shared.intent_sweeper")

SHARED_INTENTS_ARCHIVE = "shared_intents_archive"
LEARNING_EXPERIENCES = "learning_experiences"  # duplicate of the const
                                               # in learning/live_loop.py;
                                               # local const avoids a
                                               # circular import.

# Tunables (env-overridable). Defaults chosen for RISEDUAL's Atlas
# footprint — 30-min cadence, 6-hour age gate, 500-row batches.
MIN_AGE_HOURS = float(os.environ.get("INTENT_SWEEPER_MIN_AGE_HOURS", "6.0"))
BATCH_LIMIT_DEFAULT = int(os.environ.get("INTENT_SWEEPER_BATCH_LIMIT", "500"))
BATCH_LIMIT_MAX = 1000
INTERVAL_SEC = int(os.environ.get("INTENT_SWEEPER_INTERVAL_SEC", "1800"))

# 2026-02-19: scheduler ON by default. Operator can flip
# INTENT_SWEEPER_ENABLED=false at any time to pause the 30-minute
# background loop without losing the manual endpoint.
SWEEPER_ENABLED = (
    os.environ.get("INTENT_SWEEPER_ENABLED", "true").lower() == "true"
)
LEARNING_LOOP_ENABLED = (
    os.environ.get("RISE_LEARNING_LOOP_ENABLED", "true").lower() == "true"
)

ARCHIVE_VERSION = "v1"

_TASK: Optional[asyncio.Task] = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


async def _has_resolved_experience(db, intent_id: str) -> bool:
    """True iff a `learning_experiences` row for this intent has at
    least one outcome horizon resolved."""
    doc = await db[LEARNING_EXPERIENCES].find_one(
        {"intent_id": intent_id},
        {
            "_id": 0,
            "outcome_5m_bps": 1,
            "outcome_15m_bps": 1,
            "outcome_1h_bps": 1,
        },
    )
    if not doc:
        return False
    return any(
        doc.get(k) is not None
        for k in ("outcome_5m_bps", "outcome_15m_bps", "outcome_1h_bps")
    )


async def _learning_capture_exists(db, intent_id: str) -> bool:
    """True iff ANY `learning_experiences` row exists for this
    intent (resolved or not). Used to enforce the 'capture complete'
    preservation rule when the learning loop is enabled."""
    doc = await db[LEARNING_EXPERIENCES].find_one(
        {"intent_id": intent_id}, {"_id": 1},
    )
    return doc is not None


async def _has_active_capital_reservation(db, intent_id: str) -> bool:
    """True iff any lane's capital-ledger doc has an OPEN reservation
    for this intent_id. Reservations survive across pod restarts and
    the reconcile sweep can still release them, so we NEVER purge an
    intent with an active reservation.

    Query shape mirrors `shared/capital/ledger.py`:
        db[CAPITAL_LEDGER].find_one({
            "reservations": {"$elemMatch": {"intent_id": X, "status": "open"}}
        })
    """
    try:
        from namespaces import CAPITAL_LEDGER  # noqa: WPS433
    except Exception:  # noqa: BLE001
        # If the ledger namespace isn't importable in this deploy,
        # fail SAFE — assume there might be a reservation and don't
        # purge. Costs a few extra rows in the hot collection; buys
        # us zero broker-reconciliation surprises.
        return True
    try:
        doc = await db[CAPITAL_LEDGER].find_one(
            {
                "reservations": {
                    "$elemMatch": {
                        "intent_id": intent_id, "status": "open",
                    },
                },
            },
            {"_id": 1},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "intent_sweeper: capital-reservation lookup failed "
            "intent_id=%s: %s — failing safe (preserve)",
            intent_id, exc,
        )
        return True
    return doc is not None


async def sweep_stale_intents(
    db,
    *,
    dry_run: bool = True,
    batch_limit: int = BATCH_LIMIT_DEFAULT,
    _test_intent_id_prefix: Optional[str] = None,
) -> dict[str, Any]:
    """Walk one bounded batch of candidates and archive-then-delete
    (or delete outright when distilled). Returns counts + samples.

    Never raises — a query failure logs and returns partial counts.

    `_test_intent_id_prefix` is a test-only knob that scopes the
    query to intent_ids starting with the given prefix. Production
    callers leave it None. Prefixed with an underscore to make its
    non-operator status explicit.
    """
    batch_limit = max(1, min(int(batch_limit), BATCH_LIMIT_MAX))
    cutoff_iso = _iso(_now() - timedelta(hours=MIN_AGE_HOURS))

    from namespaces import SHARED_INTENTS  # local import — avoid
                                            # loading db namespace at
                                            # module import time.

    # Never-reached-broker filter — all must hold.
    q: dict[str, Any] = {
        "ingest_ts": {"$lt": cutoff_iso},
        "$or": [
            {"executed": {"$exists": False}},
            {"executed": None},
            {"executed": False},
        ],
        "$and": [
            {
                "$or": [
                    {"broker_order_id": {"$exists": False}},
                    {"broker_order_id": None},
                    {"broker_order_id": ""},
                ],
            },
            {"gate_state": {"$ne": "submitted"}},
        ],
    }
    if _test_intent_id_prefix:
        q["intent_id"] = {"$regex": f"^{_test_intent_id_prefix}"}

    counts: dict[str, Any] = {
        "scanned": 0,
        "archived": 0,
        "deleted_distilled": 0,
        "deleted_after_archive": 0,
        "archive_write_failures": 0,
        "delete_failures": 0,
        "preserved_active_reservation": 0,
        "preserved_learning_capture_incomplete": 0,
        "dry_run": bool(dry_run),
        "min_age_hours": MIN_AGE_HOURS,
        "batch_limit": batch_limit,
        "cutoff_iso": cutoff_iso,
        "samples": [],  # first few intent_ids for operator eyeballing
    }

    try:
        cursor = (
            db[SHARED_INTENTS]
            .find(q, {"_id": 0})
            .sort("ingest_ts", 1)  # oldest first
            .limit(batch_limit)
        )
        rows: list[dict] = []
        async for r in cursor:
            rows.append(r)
    except Exception as exc:  # noqa: BLE001
        logger.warning("intent_sweeper: candidate query failed: %s", exc)
        counts["error"] = f"{type(exc).__name__}: {exc}"
        return counts

    counts["scanned"] = len(rows)

    for row in rows:
        intent_id = row.get("intent_id") or ""
        if not intent_id:
            # No intent_id → cannot safely archive-then-delete. Skip.
            continue

        # PRESERVE #1: active capital-ledger reservation.
        # The reconciler could still release these; never touch.
        if await _has_active_capital_reservation(db, intent_id):
            counts["preserved_active_reservation"] += 1
            if len(counts["samples"]) < 5:
                counts["samples"].append({
                    "intent_id": intent_id,
                    "symbol": row.get("symbol"),
                    "lane": row.get("lane"),
                    "ingest_ts": row.get("ingest_ts"),
                    "gate_state": row.get("gate_state"),
                    "would_action": "preserve_active_reservation",
                })
            continue

        distilled = await _has_resolved_experience(db, intent_id)

        # PRESERVE #2: learning capture incomplete (only when learning
        # loop is enabled). If no experience row exists AT ALL, the
        # learning loop may still be catching up. Preserve.
        # Distilled rows already have an experience by definition, so
        # this check only matters for the non-distilled path.
        if (
            LEARNING_LOOP_ENABLED
            and not distilled
            and not await _learning_capture_exists(db, intent_id)
        ):
            counts["preserved_learning_capture_incomplete"] += 1
            if len(counts["samples"]) < 5:
                counts["samples"].append({
                    "intent_id": intent_id,
                    "symbol": row.get("symbol"),
                    "lane": row.get("lane"),
                    "ingest_ts": row.get("ingest_ts"),
                    "gate_state": row.get("gate_state"),
                    "would_action": "preserve_learning_capture_incomplete",
                })
            continue

        # Populate sample list for the first 5.
        if len(counts["samples"]) < 5:
            counts["samples"].append({
                "intent_id": intent_id,
                "symbol": row.get("symbol"),
                "lane": row.get("lane"),
                "ingest_ts": row.get("ingest_ts"),
                "gate_state": row.get("gate_state"),
                "would_action": (
                    "delete_distilled" if distilled else "archive_then_delete"
                ),
            })

        if dry_run:
            if distilled:
                counts["deleted_distilled"] += 1
            else:
                counts["archived"] += 1
                counts["deleted_after_archive"] += 1
            continue

        # ── LIVE PATH ─────────────────────────────────────────
        if distilled:
            # No archive — the learning tape already carries the
            # signal. Delete outright.
            try:
                del_r = await db[SHARED_INTENTS].delete_one(
                    {"intent_id": intent_id},
                )
                if del_r.deleted_count:
                    counts["deleted_distilled"] += 1
                else:
                    counts["delete_failures"] += 1
            except Exception as exc:  # noqa: BLE001
                counts["delete_failures"] += 1
                logger.warning(
                    "intent_sweeper: distilled-delete failed intent_id=%s: %s",
                    intent_id, exc,
                )
            continue

        # Archive path — write to `shared_intents_archive`, VERIFY
        # the write actually landed, THEN delete from hot.
        archive_doc = {
            **row,
            "archived_at": _iso(_now()),
            "archive_reason": "stale_never_reached_broker",
            "original_gate_state": row.get("gate_state"),
            "archive_version": ARCHIVE_VERSION,
        }
        try:
            ins_r = await db[SHARED_INTENTS_ARCHIVE].insert_one(archive_doc)
            if not ins_r.inserted_id:
                counts["archive_write_failures"] += 1
                logger.warning(
                    "intent_sweeper: archive write returned no id intent_id=%s",
                    intent_id,
                )
                continue
            counts["archived"] += 1
        except Exception as exc:  # noqa: BLE001
            counts["archive_write_failures"] += 1
            logger.warning(
                "intent_sweeper: archive write failed intent_id=%s: %s",
                intent_id, exc,
            )
            continue

        # Verify the archive row is queryable before we drop the hot row.
        try:
            verified = await db[SHARED_INTENTS_ARCHIVE].find_one(
                {"intent_id": intent_id, "archive_version": ARCHIVE_VERSION},
                {"_id": 1},
            )
        except Exception as exc:  # noqa: BLE001
            verified = None
            logger.warning(
                "intent_sweeper: archive verify query failed intent_id=%s: %s",
                intent_id, exc,
            )
        if not verified:
            counts["archive_write_failures"] += 1
            logger.warning(
                "intent_sweeper: archive verify miss intent_id=%s — "
                "aborting delete (hot row preserved)",
                intent_id,
            )
            continue

        try:
            del_r = await db[SHARED_INTENTS].delete_one(
                {"intent_id": intent_id},
            )
            if del_r.deleted_count:
                counts["deleted_after_archive"] += 1
            else:
                counts["delete_failures"] += 1
        except Exception as exc:  # noqa: BLE001
            counts["delete_failures"] += 1
            logger.warning(
                "intent_sweeper: post-archive delete failed intent_id=%s: %s",
                intent_id, exc,
            )

    if counts["scanned"]:
        logger.info(
            "intent_sweeper: dry_run=%s scanned=%d archived=%d "
            "deleted_distilled=%d deleted_after_archive=%d "
            "preserved_reservation=%d preserved_capture_incomplete=%d "
            "archive_fails=%d delete_fails=%d",
            counts["dry_run"], counts["scanned"], counts["archived"],
            counts["deleted_distilled"], counts["deleted_after_archive"],
            counts["preserved_active_reservation"],
            counts["preserved_learning_capture_incomplete"],
            counts["archive_write_failures"], counts["delete_failures"],
        )
    return counts


# ─── background loop ─────────────────────────────────────────────


async def _loop(db) -> None:
    """30-minute cadence. Runs LIVE (dry_run=False). Best-effort;
    never crashes the process."""
    logger.info(
        "intent_sweeper: LOOP STARTED interval_sec=%d min_age_hours=%.1f "
        "batch_limit=%d",
        INTERVAL_SEC, MIN_AGE_HOURS, BATCH_LIMIT_DEFAULT,
    )
    while True:
        try:
            await asyncio.sleep(INTERVAL_SEC)
            await sweep_stale_intents(
                db, dry_run=False, batch_limit=BATCH_LIMIT_DEFAULT,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("intent_sweeper: loop iteration failed: %s", exc)


def start_sweeper_if_enabled(db) -> None:
    """Create the background task. Idempotent. Respects the env
    flag `INTENT_SWEEPER_ENABLED=true` (default true)."""
    global _TASK
    if not SWEEPER_ENABLED:
        logger.info(
            "intent_sweeper: disabled (INTENT_SWEEPER_ENABLED=false)",
        )
        return
    if _TASK and not _TASK.done():
        return
    loop = asyncio.get_event_loop()
    _TASK = loop.create_task(_loop(db))


async def stop_sweeper() -> None:
    """Cancel the background task. Idempotent."""
    global _TASK
    if _TASK and not _TASK.done():
        _TASK.cancel()
        try:
            await _TASK
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    _TASK = None
