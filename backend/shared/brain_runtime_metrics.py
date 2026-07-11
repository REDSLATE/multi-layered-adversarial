"""Cached per-brain runtime metrics — Atlas-cheap replacement for
the shared_intents scan in `/api/admin/runtime/{brain}/status`.

Doctrine (2026-07-09 operator directive):

    "Runtime status should stop querying shared_intents live. Move
     status to a tiny cached heartbeat/metrics document. Update it
     when intents are written. Then status reads one small doc, not
     the huge intent tape."

Schema of `brain_runtime_metrics` (one doc per brain, `_id=brain`):
    _id            : brain canonical name (camino | barracuda | hellcat | gto)
    latest_ts      : ISO8601 of the most recent intent ingest_ts
    latest_action  : "BUY" | "SELL" | "HOLD" | ...
    latest_symbol  : "NVDA" / "BTC/USD" / ...
    last_1h        : count of intents in the last 60 min (rolling)
    last_24h       : count of intents in the last 24 hours (rolling)
    by_action      : {"BUY": n, "SELL": n, "HOLD": n} (rolling 24h)
    lifetime_count : monotonic emission counter (only ever ++)
    updated_at     : ISO8601 of the last write to THIS doc
    first_seen_at  : ISO8601 when the doc was first upserted

Writer:
    * `bump_on_emit()` — called on every `shared_intents.insert_one`
      to update the latest_* fields and increment lifetime_count.
    * `refresh_windows()` — computes last_1h / last_24h / by_action
      via a bounded aggregate query. Callable on-demand by the
      status endpoint (cached read) or a background scheduler.

Reader:
    * `get_metrics(brain)` — one small `find_one({_id: brain})` call.
      O(1) index hit vs the multi-million-row scan the old status
      endpoint did.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from db import db
from namespaces import SHARED_INTENTS

logger = logging.getLogger("brain_runtime_metrics")

COLLECTION = "brain_runtime_metrics"

# Refresh cadence: skip window recompute when a fresh recompute
# already ran within this budget. Keeps the status endpoint sub-100ms
# even under polling load (dashboard polls every ~5s per brain).
_WINDOW_REFRESH_MAX_AGE_S = 30.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def bump_on_emit(
    brain: str,
    action: Optional[str],
    symbol: Optional[str],
    ingest_ts: str,
) -> None:
    """Update the per-brain metrics doc after `shared_intents.insert_one`.

    Best-effort — a failure here MUST NEVER block intent emission,
    hence the top-level try/except swallow. Windowed counts
    (last_1h/last_24h/by_action) are NOT touched here; they are
    recomputed on-demand by `refresh_windows()` at read time. This
    keeps the write path allocation-free and avoids a second Atlas
    round-trip per intent.
    """
    if not brain:
        return
    try:
        await db[COLLECTION].update_one(
            {"_id": brain},
            {
                "$set": {
                    "latest_ts": ingest_ts,
                    "latest_action": (action or "").upper() or None,
                    "latest_symbol": symbol,
                    "updated_at": _now_iso(),
                },
                "$inc": {"lifetime_count": 1},
                "$setOnInsert": {
                    "_id": brain,
                    "first_seen_at": _now_iso(),
                },
            },
            upsert=True,
        )
    except Exception as exc:  # noqa: BLE001 — best-effort
        logger.warning("brain_runtime_metrics.bump_on_emit failed: %s", exc)


async def refresh_windows(brain: str, *, force: bool = False) -> Optional[Dict[str, Any]]:
    """Recompute last_1h / last_24h / by_action for `brain` off
    `shared_intents`, bounded to the last 24h so the query planner
    always uses the `(stack_canonical, ingest_ts)` composite index.

    Cached: if the doc's `windows_refreshed_at` is younger than
    `_WINDOW_REFRESH_MAX_AGE_S`, we return the cached doc unchanged.
    Pass `force=True` from a maintenance script to bypass.

    Returns the fresh metrics doc (post-update), or None on Atlas failure.
    """
    now = _now()
    cutoff_24h = (now - timedelta(hours=24)).isoformat()
    cutoff_1h = (now - timedelta(hours=1)).isoformat()

    # Cheap short-circuit: cached refresh still valid?
    if not force:
        try:
            existing = await db[COLLECTION].find_one({"_id": brain})
            if existing and existing.get("windows_refreshed_at"):
                try:
                    refreshed = datetime.fromisoformat(
                        existing["windows_refreshed_at"].replace("Z", "+00:00"),
                    )
                    age = (now - refreshed).total_seconds()
                    if age < _WINDOW_REFRESH_MAX_AGE_S:
                        return existing
                except (ValueError, AttributeError):
                    pass
        except Exception:  # noqa: BLE001
            pass

    # Canonicalize the brain name for the intent filter — historical
    # docs carry `stack_canonical` since the 2026-02-23 migration.
    try:
        from shared.brain_legend import canonicalize_stack  # noqa: WPS433
        brain_c = canonicalize_stack(brain) or brain
    except Exception:  # noqa: BLE001
        brain_c = brain

    last_1h: Optional[int] = None
    last_24h: Optional[int] = None
    by_action: Dict[str, int] = {}

    try:
        cursor = db[SHARED_INTENTS].aggregate(
            [
                {"$match": {
                    "stack_canonical": brain_c,
                    "ingest_ts": {"$gte": cutoff_24h},
                }},
                {"$group": {
                    "_id": "$action",
                    "count": {"$sum": 1},
                    "recent_1h": {
                        "$sum": {
                            "$cond": [
                                {"$gte": ["$ingest_ts", cutoff_1h]},
                                1, 0,
                            ],
                        },
                    },
                }},
            ],
        )
        total_24h = 0
        total_1h = 0
        async for row in cursor:
            action_key = str(row.get("_id") or "UNK").upper()
            cnt_24h = int(row.get("count", 0))
            cnt_1h = int(row.get("recent_1h", 0))
            total_24h += cnt_24h
            total_1h += cnt_1h
            by_action[action_key] = cnt_24h
        last_24h = total_24h
        last_1h = total_1h
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "brain_runtime_metrics.refresh_windows aggregate failed "
            "brain=%s err=%s", brain, exc,
        )
        return None

    try:
        await db[COLLECTION].update_one(
            {"_id": brain},
            {
                "$set": {
                    "last_1h": last_1h,
                    "last_24h": last_24h,
                    "by_action": by_action,
                    "windows_refreshed_at": _now_iso(),
                    "updated_at": _now_iso(),
                },
                "$setOnInsert": {
                    "_id": brain,
                    "first_seen_at": _now_iso(),
                },
            },
            upsert=True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "brain_runtime_metrics.refresh_windows update failed "
            "brain=%s err=%s", brain, exc,
        )
        return None

    try:
        return await db[COLLECTION].find_one({"_id": brain})
    except Exception:  # noqa: BLE001
        return None


async def get_metrics(brain: str) -> Optional[Dict[str, Any]]:
    """Read the cached metrics doc. Returns None if the brain has
    never emitted (no doc exists yet)."""
    if not brain:
        return None
    try:
        return await db[COLLECTION].find_one({"_id": brain})
    except Exception as exc:  # noqa: BLE001
        logger.warning("brain_runtime_metrics.get_metrics failed: %s", exc)
        return None


# ═══════════════════════════════════════════════════════════════════
#  Stack-level cache (2026-02-19 operator directive)
# ═══════════════════════════════════════════════════════════════════
#
# "One stack → one heartbeat/status document → four brain sections
#  → one UI poll." The BrainConsole used to hit
#  `/admin/runtime/{brain}/status` 4× (once per brain), each of which
#  fanned out into ~5 Atlas queries. Under Atlas load this caused all
#  four brain pages to display the SAME NetworkTimeout — because they
#  were all hammering the same overloaded collection.
#
# The stack document `_id=risedual_stack` is a single compact doc
# holding one section per brain. `/api/admin/runtime/stack/status`
# reads it with a single indexed lookup. Any status writer (intent
# emission, heartbeat) updates the appropriate `brains.<name>.*`
# subfields via `$set` — no lock contention because writes target
# disjoint subpaths.

_STACK_ID = "risedual_stack"


async def bump_stack_on_emit(
    brain: str,
    action: Optional[str],
    symbol: Optional[str],
    ingest_ts: str,
) -> None:
    """Update the stack-level status doc's brain section on an intent
    emission. Called from the same site as `bump_on_emit` — same
    best-effort contract (never blocks emission).
    """
    if not brain:
        return
    try:
        await db[COLLECTION].update_one(
            {"_id": _STACK_ID},
            {
                "$set": {
                    f"brains.{brain}.latest_intent_ts": ingest_ts,
                    f"brains.{brain}.latest_action": (
                        (action or "").upper() or None
                    ),
                    f"brains.{brain}.latest_symbol": symbol,
                    f"brains.{brain}.updated_at": _now_iso(),
                    "updated_at": _now_iso(),
                    "stack_status": "healthy",
                },
                "$inc": {f"brains.{brain}.lifetime_count": 1},
                "$setOnInsert": {
                    "_id": _STACK_ID,
                    "first_seen_at": _now_iso(),
                },
            },
            upsert=True,
        )
    except Exception as exc:  # noqa: BLE001 — best-effort
        logger.warning(
            "brain_runtime_metrics.bump_stack_on_emit failed: %s", exc,
        )


async def bump_stack_heartbeat(brain: str, heartbeat_ts: str) -> None:
    """Refresh the stack doc's `brains.<brain>.last_heartbeat_ts` without
    touching decision or intent-write fields.

    Doctrine (2026-02-20, operator directive — "3 clocks"):
        Heartbeat freshness only proves the runner loop is alive. It
        says nothing about whether decisions are being made or whether
        Mongo inserts are landing. `last_heartbeat_ts` is one of three
        clocks the console tracks; the others are `last_decision_ts`
        (bumped by `bump_stack_decision`) and
        `last_db_confirmed_intent_ts` (bumped inside the write path
        after a confirmed `insert_one`).

    The legacy `heartbeat_ts` key is kept as an alias for one
    deprecation cycle so any older consumer reading it still works.
    """
    if not brain:
        return
    try:
        await db[COLLECTION].update_one(
            {"_id": _STACK_ID},
            {
                "$set": {
                    f"brains.{brain}.last_heartbeat_ts": heartbeat_ts,
                    f"brains.{brain}.heartbeat_ts": heartbeat_ts,  # legacy alias
                    "updated_at": _now_iso(),
                },
                "$setOnInsert": {
                    "_id": _STACK_ID,
                    "first_seen_at": _now_iso(),
                },
            },
            upsert=True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "brain_runtime_metrics.bump_stack_heartbeat failed: %s", exc,
        )


# ═══════════════════════════════════════════════════════════════════
#  3-clock intent-write health (2026-02-20 operator directive)
# ═══════════════════════════════════════════════════════════════════
#
# Splitting the brain-side truth from the DB-side truth:
#
#   * last_heartbeat_ts             — runner tick is alive
#   * last_decision_ts              — runner produced a decision this
#                                     tick (BUY/SELL/HOLD; direction
#                                     is captured in
#                                     `last_decision_action`)
#   * last_db_confirmed_intent_ts   — Mongo `insert_one` succeeded
#                                     for ANY action (HOLD included).
#                                     A brain that legitimately emits
#                                     HOLD for an hour is STILL
#                                     writing to Mongo — this is the
#                                     honest signal that the write
#                                     path is alive.
#   * last_db_confirmed_directional_intent_ts
#                                   — same, but only for
#                                     BUY/SELL/SHORT/COVER. Separated
#                                     so the console can distinguish
#                                     "writer healthy but no
#                                     directional opportunity" from
#                                     "writer dead".
#
# Counters (monotonic $inc, cumulative since first_seen_at):
#   decisions_total
#   intent_submit_attempts_total
#   intent_submit_successes_total
#   directional_submit_successes_total
#   intent_submit_failures_total
#
# All helpers are best-effort — a failure here MUST NEVER interfere
# with the actual write path. The caller in `shared/intents.py`
# handles insert failures by RE-RAISING to the runner (see doctrine
# note there).


DIRECTIONAL_ACTIONS = frozenset({"BUY", "SELL", "SHORT", "COVER"})


async def bump_stack_decision(
    brain: str,
    action: Optional[str],
    symbol: Optional[str],
) -> None:
    """Runner produced a decision this tick (pre-write).

    Bumps `last_decision_ts` and `decisions_total`. Records the
    decided action/symbol so the operator can distinguish "brain is
    running but only ever holds" from "brain hasn't ticked in an
    hour".
    """
    if not brain:
        return
    try:
        await db[COLLECTION].update_one(
            {"_id": _STACK_ID},
            {
                "$set": {
                    f"brains.{brain}.last_decision_ts": _now_iso(),
                    f"brains.{brain}.last_decision_action": (
                        (action or "").upper() or None
                    ),
                    f"brains.{brain}.last_decision_symbol": symbol,
                    "updated_at": _now_iso(),
                },
                "$inc": {f"brains.{brain}.decisions_total": 1},
                "$setOnInsert": {
                    "_id": _STACK_ID,
                    "first_seen_at": _now_iso(),
                },
            },
            upsert=True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "brain_runtime_metrics.bump_stack_decision failed: %s", exc,
        )


async def bump_stack_intent_attempt(brain: str) -> None:
    """Increment `intent_submit_attempts_total` for `brain`. Called
    IMMEDIATELY before the Mongo `insert_one`. Paired with either a
    success bump or a failure bump — never both, never neither."""
    if not brain:
        return
    try:
        await db[COLLECTION].update_one(
            {"_id": _STACK_ID},
            {
                "$inc": {f"brains.{brain}.intent_submit_attempts_total": 1},
                "$set": {
                    f"brains.{brain}.last_intent_submit_attempt_ts": _now_iso(),
                    "updated_at": _now_iso(),
                },
                "$setOnInsert": {
                    "_id": _STACK_ID,
                    "first_seen_at": _now_iso(),
                },
            },
            upsert=True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "brain_runtime_metrics.bump_stack_intent_attempt failed: %s", exc,
        )


async def bump_stack_intent_success(
    brain: str,
    *,
    intent_id: Optional[str] = None,
    mongo_id: Optional[str] = None,
    action: Optional[str] = None,
    symbol: Optional[str] = None,
    lane: Optional[str] = None,
    ingest_ts: Optional[str] = None,
) -> None:
    """Stamp a tiny write receipt after a CONFIRMED Mongo insert.

    Fields set (per operator's 2026-02-20 spec):
        * `last_db_confirmed_intent_ts`         — always
        * `last_db_confirmed_directional_intent_ts` — only for
                                                  BUY/SELL/SHORT/COVER

    Also records a compact receipt (`last_write_receipt`) so the
    operator can eyeball WHICH intent last landed — invaluable when
    reconciling with the `shared_intents` tape.

    Counters $inc'd:
        * `intent_submit_successes_total`         — always
        * `directional_submit_successes_total`    — directional only
    """
    if not brain:
        return
    action_u = (action or "").upper() or None
    is_directional = action_u in DIRECTIONAL_ACTIONS
    now = _now_iso()
    write_ts = ingest_ts or now
    receipt = {
        "intent_id": intent_id,
        "mongo_id": mongo_id,
        "action": action_u,
        "symbol": symbol,
        "lane": lane,
        "ingest_ts": write_ts,
        "recorded_at": now,
    }
    set_fields: Dict[str, Any] = {
        f"brains.{brain}.last_db_confirmed_intent_ts": write_ts,
        f"brains.{brain}.last_write_receipt": receipt,
        "updated_at": now,
    }
    inc_fields: Dict[str, Any] = {
        f"brains.{brain}.intent_submit_successes_total": 1,
    }
    if is_directional:
        set_fields[
            f"brains.{brain}.last_db_confirmed_directional_intent_ts"
        ] = write_ts
        inc_fields[f"brains.{brain}.directional_submit_successes_total"] = 1
    try:
        await db[COLLECTION].update_one(
            {"_id": _STACK_ID},
            {
                "$set": set_fields,
                "$inc": inc_fields,
                "$setOnInsert": {
                    "_id": _STACK_ID,
                    "first_seen_at": now,
                },
            },
            upsert=True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "brain_runtime_metrics.bump_stack_intent_success failed: %s", exc,
        )


async def bump_stack_intent_failure(
    brain: str,
    *,
    error: Optional[str] = None,
    action: Optional[str] = None,
    symbol: Optional[str] = None,
) -> None:
    """Record a FAILED `shared_intents.insert_one`. Increments
    `intent_submit_failures_total` and stamps `last_intent_submit_error`
    so the operator can see the failure mode without scanning logs.

    Doctrine: this helper is best-effort. The CALLER is responsible
    for re-raising the underlying exception so the runner learns the
    submit did not persist — silencing a write failure here is the
    dishonesty the 3-clock design was built to eliminate.
    """
    if not brain:
        return
    err_msg = (str(error) if error is not None else "unknown")[:400]
    now = _now_iso()
    try:
        await db[COLLECTION].update_one(
            {"_id": _STACK_ID},
            {
                "$set": {
                    f"brains.{brain}.last_intent_submit_error_ts": now,
                    f"brains.{brain}.last_intent_submit_error_msg": err_msg,
                    f"brains.{brain}.last_intent_submit_error_action": (
                        (action or "").upper() or None
                    ),
                    f"brains.{brain}.last_intent_submit_error_symbol": symbol,
                    "updated_at": now,
                },
                "$inc": {
                    f"brains.{brain}.intent_submit_failures_total": 1,
                },
                "$setOnInsert": {
                    "_id": _STACK_ID,
                    "first_seen_at": now,
                },
            },
            upsert=True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "brain_runtime_metrics.bump_stack_intent_failure failed: %s", exc,
        )


async def get_stack_status() -> Optional[Dict[str, Any]]:
    """One-read stack status. Default-hostile: any Atlas failure
    surfaces as None so the endpoint can return an amber
    `degraded=true` response instead of a red banner. The single
    lookup is O(1) against the `_id` primary key — no collection
    scan, no aggregate, no time-window filter."""
    try:
        return await db[COLLECTION].find_one({"_id": _STACK_ID})
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "brain_runtime_metrics.get_stack_status failed: %s", exc,
        )
        return None
