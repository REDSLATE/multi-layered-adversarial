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

  3. LEARNING-CAPTURE-REQUIRED CLASSIFIER (operator directive
     2026-02-19 refinement):

         def learning_capture_required(row):
             action = (row.execution.action or row.action or "").upper()
             reached_execution = (
                 row.broker_order_id is not None
                 or row.gate_state in {"submitted", "executed",
                                       "broker_rejected"}
             )
             return (
                 action in {"BUY", "SELL", "SHORT", "COVER"}
                 and reached_execution
             )

     The live-learning predicate only captures REAL directional
     exposure — a HOLD, WATCH, or blocked-before-broker no_trade row
     was never supposed to enter the learning tape. Requiring a
     learning record on those was categorically wrong and created
     permanent retention.

  4. NEVER PURGE — safety carve-outs enforced per-row after query:
       * Anything with an active capital-ledger reservation
         (`capital_ledger.reservations[].status == "open"` for the
         intent_id). The reconciler could still release these.
       * IF `learning_capture_required(row)` AND no
         `learning_experiences` row exists yet — the learning loop
         may still be catching up. Preserve. (Non-directional rows
         and blocked-pre-broker rows do NOT trigger this preserve;
         they were never expected to have a learning record.)

  5. LEARNING-AWARE BIFURCATION — for each surviving candidate:
       * If a resolved `learning_experience` exists (at least one
         of `outcome_5m_bps`, `outcome_15m_bps`, `outcome_1h_bps`
         is set), the intent's information has been distilled into
         the learning tape. DELETE outright — no archive needed.
         Reason: `distilled_via_learning_experience`.
       * Otherwise, ARCHIVE the row to `shared_intents_archive`
         with the doctrine-mandated stamps, verify the write
         actually landed, THEN delete from the hot collection.
         `archive_reason` is typed by category:
           - `legacy_non_learning_no_trade` — HOLD/WATCH/no_trade
             row that never reached broker
           - `directional_blocked_pre_broker` — BUY/SELL that was
             blocked upstream of the broker (kept in archive for
             counterfactual/missed-trade analysis)
           - `stale_never_reached_broker` — generic fallback
             (should be rare with the classifier in place)

  6. BATCH BOUNDED — default 500, capped 1000. Prevents a single
     invocation from hammering Atlas with a massive delete.

  7. DRY RUN — the admin endpoint defaults to `dry_run=true`.
     Prints what would be affected without touching Mongo.

     Counts split by category so operator can eyeball semantics:
         matched                          — query-level candidates
         learning_required                — classifier == True
         learning_not_applicable          — classifier == False
         preserved_missing_learning       — required + no row
         preserved_active_reservation     — capital ledger open
         eligible_for_purge               — actually processed
         (of which:)
           archived                       — archived+deleted
           deleted_distilled              — outright delete
           archive_write_failures
           delete_failures

  8. SCHEDULER ON BY DEFAULT — 30-minute background loop runs
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
    """True iff ANY `learning_experiences` row exists for this intent
    (resolved or not). Used with `learning_capture_required()` to
    enforce the 'still catching up' preservation rule."""
    doc = await db[LEARNING_EXPERIENCES].find_one(
        {"intent_id": intent_id}, {"_id": 1},
    )
    return doc is not None


_DIRECTIONAL_ACTIONS = {"BUY", "SELL", "SHORT", "COVER"}
_REACHED_EXECUTION_GATE_STATES = {
    "submitted", "executed", "broker_rejected",
}


def learning_capture_required(intent: dict) -> bool:
    """The live-learning predicate: only DIRECTIONAL intents that
    actually REACHED execution are supposed to have a learning row.

    2026-02-19 operator directive — the pre-refinement rule that
    required a learning record for every stale intent was
    categorically wrong for HOLD/WATCH/no_trade rows, which the
    learning loop was never designed to capture.

    Returns True iff:
        action in {BUY, SELL, SHORT, COVER}
        AND (broker_order_id set OR gate_state in
             {submitted, executed, broker_rejected})
    """
    exec_block = intent.get("execution") or {}
    action_raw = (
        (exec_block.get("action") if isinstance(exec_block, dict) else None)
        or intent.get("action")
        or ""
    )
    action = str(action_raw).upper()
    if action not in _DIRECTIONAL_ACTIONS:
        return False
    reached = bool(intent.get("broker_order_id")) or (
        intent.get("gate_state") in _REACHED_EXECUTION_GATE_STATES
    )
    return reached


def _classify_archive_reason(intent: dict) -> str:
    """Category label for the archive row's `archive_reason` field.

    * `legacy_non_learning_no_trade` — HOLD/WATCH/no_trade row that
      never reached broker (dominant category by count).
    * `directional_blocked_pre_broker` — BUY/SELL/SHORT/COVER that
      was blocked upstream of the broker. Kept for counterfactual /
      missed-trade analysis.
    * `stale_never_reached_broker` — generic fallback.
    """
    exec_block = intent.get("execution") or {}
    action = str(
        (exec_block.get("action") if isinstance(exec_block, dict) else None)
        or intent.get("action") or ""
    ).upper()
    gate_state = str(intent.get("gate_state") or "").lower()

    if action not in _DIRECTIONAL_ACTIONS or gate_state in {
        "hold", "watch", "no_trade", "advisory_only",
    }:
        return "legacy_non_learning_no_trade"
    if action in _DIRECTIONAL_ACTIONS:
        return "directional_blocked_pre_broker"
    return "stale_never_reached_broker"


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
        # Query-level candidates that passed the age gate + never-
        # reached-broker filter.
        "matched": 0,
        "scanned": 0,  # alias for matched (legacy)
        # Classification breakdown (operator directive 2026-02-19).
        "learning_required": 0,
        "learning_not_applicable": 0,
        # Preserve counters — rows we intentionally did NOT touch.
        "preserved_active_reservation": 0,
        "preserved_missing_learning": 0,
        # Action counters — rows we processed.
        "eligible_for_purge": 0,
        "archived": 0,
        "deleted_distilled": 0,
        "deleted_after_archive": 0,
        # Failure counters.
        "archive_write_failures": 0,
        "delete_failures": 0,
        # Config echo.
        "dry_run": bool(dry_run),
        "min_age_hours": MIN_AGE_HOURS,
        "batch_limit": batch_limit,
        "cutoff_iso": cutoff_iso,
        # Per-reason archive breakdown (would-archive under dry-run).
        "archive_reason_breakdown": {
            "legacy_non_learning_no_trade": 0,
            "directional_blocked_pre_broker": 0,
            "stale_never_reached_broker": 0,
        },
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
    counts["matched"] = len(rows)

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
                    "action": row.get("action"),
                    "ingest_ts": row.get("ingest_ts"),
                    "gate_state": row.get("gate_state"),
                    "would_action": "preserve_active_reservation",
                })
            continue

        # Classify: does this row need a learning record?
        needs_learning = learning_capture_required(row)
        if needs_learning:
            counts["learning_required"] += 1
        else:
            counts["learning_not_applicable"] += 1

        distilled = await _has_resolved_experience(db, intent_id)

        # PRESERVE #2: learning REQUIRED but not yet captured.
        # ONLY applies to directional rows that reached execution.
        # HOLD/WATCH/blocked-pre-broker rows are NEVER expected to
        # have a learning record and must not be preserved on this
        # rule. (This is the 2026-02-19 refinement — the earlier
        # blanket rule created permanent retention for no_trade
        # rows.)
        if (
            LEARNING_LOOP_ENABLED
            and needs_learning
            and not distilled
            and not await _learning_capture_exists(db, intent_id)
        ):
            counts["preserved_missing_learning"] += 1
            if len(counts["samples"]) < 5:
                counts["samples"].append({
                    "intent_id": intent_id,
                    "symbol": row.get("symbol"),
                    "lane": row.get("lane"),
                    "action": row.get("action"),
                    "ingest_ts": row.get("ingest_ts"),
                    "gate_state": row.get("gate_state"),
                    "would_action": "preserve_missing_learning",
                })
            continue

        # Row is eligible for purge from here on.
        counts["eligible_for_purge"] += 1
        archive_reason = (
            None if distilled  # distilled → no archive
            else _classify_archive_reason(row)
        )
        if archive_reason and archive_reason in counts["archive_reason_breakdown"]:
            counts["archive_reason_breakdown"][archive_reason] += 1

        # 2026-02-19 operator directive — distill directional-blocked
        # rows into a `counterfactual_signals` record BEFORE we drop
        # the raw intent. The signal is a compact, resolvable
        # learning artefact ("would this trade have worked?"). If
        # distillation fails (missing reference price, etc.) we
        # preserve the raw intent instead of losing the signal.
        #
        # Two-stage `purge_state="distilling"` protocol (2026-02-19
        # upgrade): stamp the intent BEFORE calling distill so a
        # concurrent sweeper can't race the same row through.
        # Delete only after the distill returns success AND the
        # stamp is still present — a mid-flight crash or exception
        # leaves the raw intent intact for the next sweep to retry.
        will_distill = (
            not dry_run
            and archive_reason == "directional_blocked_pre_broker"
        )
        if will_distill:
            distilled_ok = False
            distill_stamped = False
            try:
                stamp_r = await db[SHARED_INTENTS].update_one(
                    {"intent_id": intent_id,
                     "purge_state": {"$in": [None, "eligible"]}},
                    {"$set": {
                        "purge_state": "distilling",
                        "purge_state_ts": _iso(_now()),
                    }},
                )
                distill_stamped = bool(stamp_r.modified_count)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "intent_sweeper: purge_state stamp failed "
                    "intent_id=%s: %s",
                    intent_id, exc,
                )
            if not distill_stamped:
                # Either the row already carries a `purge_state`
                # from a concurrent sweep, or the update raced. Skip
                # for this pass; the row survives for the next one.
                counts["preserved_missing_learning"] += 1
                counts["eligible_for_purge"] -= 1
                if archive_reason in counts["archive_reason_breakdown"]:
                    counts["archive_reason_breakdown"][archive_reason] -= 1
                continue
            try:
                from shared.counterfactuals import (  # noqa: WPS433
                    distill_intent_to_signal,
                )
                distilled_ok = await distill_intent_to_signal(row, db)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "intent_sweeper: counterfactual distill failed "
                    "intent_id=%s: %s",
                    intent_id, exc,
                )
                distilled_ok = False
            if not distilled_ok:
                # Clear the distilling flag so the next sweep can
                # retry this row cleanly.
                try:
                    await db[SHARED_INTENTS].update_one(
                        {"intent_id": intent_id,
                         "purge_state": "distilling"},
                        {"$unset": {"purge_state": "",
                                    "purge_state_ts": ""}},
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "intent_sweeper: purge_state clear failed "
                        "intent_id=%s: %s",
                        intent_id, exc,
                    )
                counts["preserved_missing_learning"] += 1
                if len(counts["samples"]) < 5:
                    counts["samples"].append({
                        "intent_id": intent_id,
                        "symbol": row.get("symbol"),
                        "lane": row.get("lane"),
                        "action": row.get("action"),
                        "ingest_ts": row.get("ingest_ts"),
                        "gate_state": row.get("gate_state"),
                        "would_action": "preserve_distill_failed",
                    })
                # roll back the eligibility counter — we're not
                # actually purging this row.
                counts["eligible_for_purge"] -= 1
                if archive_reason in counts["archive_reason_breakdown"]:
                    counts["archive_reason_breakdown"][archive_reason] -= 1
                continue

        # Populate sample list for the first 5 (only for rows we
        # will actually touch).
        if len(counts["samples"]) < 5:
            counts["samples"].append({
                "intent_id": intent_id,
                "symbol": row.get("symbol"),
                "lane": row.get("lane"),
                "action": row.get("action"),
                "ingest_ts": row.get("ingest_ts"),
                "gate_state": row.get("gate_state"),
                "would_action": (
                    "delete_distilled" if distilled
                    else f"archive_then_delete:{archive_reason}"
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

        # Counterfactual-distilled path — the raw intent already
        # carries `purge_state="distilling"` from the two-stage
        # stamp. Delete only if the stamp is still ours; if a
        # concurrent process cleared it, leave the row alone.
        if will_distill:
            try:
                del_r = await db[SHARED_INTENTS].delete_one(
                    {"intent_id": intent_id,
                     "purge_state": "distilling"},
                )
                if del_r.deleted_count:
                    counts["deleted_distilled"] += 1
                else:
                    counts["delete_failures"] += 1
            except Exception as exc:  # noqa: BLE001
                counts["delete_failures"] += 1
                logger.warning(
                    "intent_sweeper: counterfactual-delete failed intent_id=%s: %s",
                    intent_id, exc,
                )
            continue

        # Archive path — write to `shared_intents_archive`, VERIFY
        # the write actually landed, THEN delete from hot.
        archive_doc = {
            **row,
            "archived_at": _iso(_now()),
            "archive_reason": (
                archive_reason or "stale_never_reached_broker"
            ),
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

    if counts["matched"]:
        logger.info(
            "intent_sweeper: dry_run=%s matched=%d "
            "learning_req=%d learning_na=%d "
            "preserved(reservation=%d missing_learning=%d) "
            "eligible=%d archived=%d deleted_distilled=%d "
            "deleted_after_archive=%d "
            "archive_fails=%d delete_fails=%d "
            "reasons=%s",
            counts["dry_run"], counts["matched"],
            counts["learning_required"], counts["learning_not_applicable"],
            counts["preserved_active_reservation"],
            counts["preserved_missing_learning"],
            counts["eligible_for_purge"],
            counts["archived"], counts["deleted_distilled"],
            counts["deleted_after_archive"],
            counts["archive_write_failures"], counts["delete_failures"],
            counts["archive_reason_breakdown"],
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
