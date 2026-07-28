"""Auto-router reconciliation & expiration sweeps — extracted from
`shared/auto_router.py` on 2026-02-19 to shrink the main file (was
1761 lines).

Everything here is called from `auto_router._tick()` OR the
supervisor task; nothing in this module calls back into the
routing hot path (`_route_one`). Module state is scoped to
reconciliation-only concerns.

Public API (re-exported from `shared.auto_router` for backward
compatibility with existing callers and tests):

    _sweep_expired_unrouted() -> int
    _sweep_submitted_broker_orders() -> dict
    _finish_sweep(counts) -> dict          (internal reconcile helper)
    _minutes_since_iso(iso, now_utc)       (helper used by _tick)
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from db import db
from namespaces import SHARED_GATE_RESULTS, SHARED_INTENTS

logger = logging.getLogger("auto_router.reconciliation")

# ── Reconciliation tunables (2026-07-06) ──────────────────────────
RECONCILE_MAX_RETRIES = 3
RECONCILE_MIN_AGE_SEC = 30
RECONCILE_BATCH_CAP = 25
RECONCILE_BOUNDARY_WARN_MIN = 45  # 75% of default 60min lookback
RECONCILE_MIN_INTERVAL_SEC = 25
_LAST_RECONCILE_SWEEP_TS: Optional[datetime] = None
# 2026-07-28: unfilled entry orders are cancelled after this many
# minutes (LCID stale-limit autopsy). Env-tunable, no redeploy.
ENTRY_ORDER_TTL_MIN = float(os.environ.get("ENTRY_ORDER_TTL_MIN", "10"))
# 2026-07-28: pending (never-routed) intents expire after this many
# minutes — stale momentum + they clog the arbiter dedupe guard.
PENDING_INTENT_TTL_MIN = float(os.environ.get("PENDING_INTENT_TTL_MIN", "30"))

# Expiration-sweep default. Duplicates the constant in `auto_router.py`
# so this module doesn't depend on the main file's globals at import
# time (env-driven override still honoured at call time).
AUTO_ROUTER_EXPIRE_MIN = int(os.environ.get("AUTO_ROUTER_EXPIRE_MIN", "120"))
AUTO_ROUTER_EMAIL = "auto-router@mission-control"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _sweep_expired_unrouted() -> int:
    """Terminally stamp intents older than `AUTO_ROUTER_EXPIRE_MIN` that
    were never routed to a terminal state.

    Rationale (2026-02-28): `_tick` only SAMPLES within
    `AUTO_ROUTER_LOOKBACK_MIN` (60 min by default). Anything older that
    hadn't already been stamped `blocked` / `advisory_only` / `submitted`
    silently vanished from the funnel — the operator saw the intent
    emitted, then nothing. This sweeper closes that gap by writing an
    explicit `gate_state=expired_unrouted` on age-outs.

    Default expire window is DOUBLE the lookback (120 min vs 60 min) so
    a legitimate late-arriving intent isn't cut off by racing the two
    windows. Returns the number of intents stamped this pass.
    """
    try:
        expire_min = int(os.environ.get("AUTO_ROUTER_EXPIRE_MIN",
                                        str(AUTO_ROUTER_EXPIRE_MIN)))
    except (TypeError, ValueError):
        expire_min = AUTO_ROUTER_EXPIRE_MIN
    expire_cutoff = (
        datetime.now(timezone.utc) - timedelta(minutes=expire_min)
    ).isoformat()
    try:
        # 2026-02-28: hard batch cap + Mongo-side deadline. On prod's
        # multi-million-row `shared_intents`, an unbounded update_many
        # over `ingest_ts < cutoff` could scan a huge slice and blow
        # past the 12s asyncio timeout below. We DELIBERATELY cap at
        # 500 stamps per tick — old-and-not-terminal intents leak
        # slowly over successive ticks instead of one giant sweep
        # that could starve the routing scan (same collection).
        # `max_time_ms(3000)` fails at the DB layer if the query
        # can't complete within 3s, well under the asyncio deadline.
        expired_ids: list[str] = []
        cur = (
            db[SHARED_INTENTS]
            .find(
                {
                    "ingest_ts": {"$lt": expire_cutoff},
                    "executed": {"$ne": True},
                    "gate_state": {"$nin": [
                        "blocked", "no_trade", "advisory_only",
                        "submitted", "expired_unrouted",
                    ]},
                },
                {"intent_id": 1, "_id": 0},
            )
            .max_time_ms(3000)
            .limit(500)
        )
        async for d in cur:
            if d.get("intent_id"):
                expired_ids.append(d["intent_id"])
        if not expired_ids:
            return 0
        result = await asyncio.wait_for(
            db[SHARED_INTENTS].update_many(
                {"intent_id": {"$in": expired_ids}},
                {"$set": {
                    "gate_state": "expired_unrouted",
                    "expired_at": _now_iso(),
                    "expired_by": AUTO_ROUTER_EMAIL,
                    "expire_reason": (
                        f"aged_past_router_window:{expire_min}min"
                    ),
                    # Doctrine 2026-07-12 (Step 7): mirror into
                    # `broker_reason` so operator dashboards
                    # keyed on that field see the reason without
                    # a client-side field-name pivot.
                    "broker_reason": "EXPIRED_UNROUTED",
                    "broker_error_bucket": "queue_timeout",
                    "broker_error_detail": (
                        f"aged_past_router_window:{expire_min}min"
                    ),
                }},
            ),
            timeout=5.0,
        )
        stamped = int(result.modified_count or 0)
        if stamped:
            logger.info(
                "auto_router expired_unrouted sweep stamped %d intents "
                "older than %d min", stamped, expire_min,
            )
        return stamped
    except Exception as exc:  # noqa: BLE001
        logger.warning("expired_unrouted sweep failed: %s", exc)
        return 0
    finally:
        # Second pass (2026-07-28 operator directive): PENDING-intent
        # TTL. A momentum intent still unrouted after
        # PENDING_INTENT_TTL_MIN is stale — it also clogs the
        # arbiter's duplicate-stance guard (a lingering pending BUY/
        # SELL blocks every re-emission for that brain+symbol+action,
        # observed silting the emission path with 23 stuck rows).
        try:
            await _sweep_stale_pending()
        except Exception as exc:  # noqa: BLE001
            logger.warning("stale-pending sweep failed: %s", exc)


async def _sweep_stale_pending() -> int:
    """Terminal-expire `gate_state=pending` intents older than
    PENDING_INTENT_TTL_MIN (default 30). Never touches submitted /
    filled / blocked rows. Batch-capped like the main sweep."""
    cutoff = (
        datetime.now(timezone.utc)
        - timedelta(minutes=PENDING_INTENT_TTL_MIN)
    ).isoformat()
    ids: list[str] = []
    cur = (
        db[SHARED_INTENTS]
        .find(
            {
                "gate_state": "pending",
                "executed": {"$ne": True},
                "ingest_ts": {"$lt": cutoff},
            },
            {"intent_id": 1, "_id": 0},
        )
        .max_time_ms(3000)
        .limit(500)
    )
    async for d in cur:
        if d.get("intent_id"):
            ids.append(d["intent_id"])
    if not ids:
        return 0
    result = await asyncio.wait_for(
        db[SHARED_INTENTS].update_many(
            {"intent_id": {"$in": ids}, "gate_state": "pending"},
            {"$set": {
                "gate_state": "expired_unrouted",
                "expired_at": _now_iso(),
                "expired_by": AUTO_ROUTER_EMAIL,
                "expire_reason": (
                    f"pending_ttl:{PENDING_INTENT_TTL_MIN:.0f}min"
                ),
                "broker_reason": "EXPIRED_PENDING_TTL",
                "broker_error_bucket": "queue_timeout",
                "broker_error_detail": (
                    f"pending_unrouted_past_{PENDING_INTENT_TTL_MIN:.0f}min"
                ),
            }},
        ),
        timeout=5.0,
    )
    stamped = int(result.modified_count or 0)
    if stamped:
        logger.info(
            "pending-TTL sweep expired %d intents older than %.0f min",
            stamped, PENDING_INTENT_TTL_MIN,
        )
    return stamped


# ─── Broker reconciliation sweep (2026-07-06) ─────────────────────────
# Poll Webull for the current status of intents MC submitted but whose
# fills/rejects haven't been observed. Fixes P1 stuck-`submitted` bug
# from the handoff: intents that got submitted but later filled or
# rejected by the broker were never transitioning to their terminal
# state — MC had no closed-loop reconciliation.
#
# Design pins (operator sign-off 2026-07-06):
#   * Equity lane only. Kraken adapter lacks `get_order` symmetry;
#     crypto reconciliation is a separate task.
#   * Skip fresh submits (<30s old) — broker hasn't touched them.
#   * Cap 25 intents per tick to bound Webull API calls under 429 risk.
#   * `.max_time_ms(3000)` bounding, `asyncio.wait_for` timeout guards.
#   * On rejection: classify() via existing broker_error_taxonomy —
#     terminal buckets (insufficient_funds, market_closed, etc.) go
#     straight to `broker_rejected`; transient buckets get retried up
#     to RECONCILE_MAX_RETRIES (3), then also go terminal.
#   * On retry: flip `gate_state='pending'` (the canonical fresh-
#     emission state; the pipeline treats it identically). Preserve
#     `ingest_ts` — an aged-out `pending` intent still gets caught by
#     `_sweep_expired_unrouted` at 120min. Between 60min and 120min
#     it's in a stall zone but NOT silent. Log a WARNING when a
#     requeue happens on an intent already past 75% of the lookback
#     window so the operator can spot slow retry cycles.
#   * On Filled: update the intent doc only — do NOT write a new
#     executions row. The original submit-time row is the audit;
#     reconciliation just updates the fill fields.
RECONCILE_MAX_RETRIES = 3
RECONCILE_MIN_AGE_SEC = 30
RECONCILE_BATCH_CAP = 25
RECONCILE_BOUNDARY_WARN_MIN = 45  # 75% of default 60min lookback
# Minimum wall-clock gap between two sweep runs. The auto_router
# scheduled tick fires every 30s (AUTO_ROUTER_INTERVAL_SEC), but
# `force_one_tick()` is ALSO invoked out-of-band on every intent
# insert (see shared/intents.py:_run_auto_router_kick — a ~50ms
# latency optimization for fresh brain emissions). Without this
# gate, a burst of 5 intents in 7s would trigger 5 back-to-back
# reconcile sweeps → 5N Webull `get_order` calls → HTTP 429. This
# gate keeps the scheduled 30s cadence but skips the redundant
# kicker-triggered runs (2026-07-06 smoke-test finding).
RECONCILE_MIN_INTERVAL_SEC = 25
_LAST_RECONCILE_SWEEP_TS: Optional[datetime] = None


async def _sweep_submitted_broker_orders() -> dict:
    """Poll each broker for the current status of `gate_state='submitted'`
    intents. Transitions them to `filled`, `broker_rejected`, or (on
    transient reject under retry cap) back to `pending` for re-routing
    on the next tick.

    2026-07-09 iter-22 — crypto sweep landing (P2 backlog):
        Historically this function only queried Webull (`lane=equity`),
        leaving Kraken-submitted intents stuck in `submitted` state
        forever. As of iter-22 both adapters are polled — `KrakenLive
        Adapter.get_order(txid)` returns the same normalized shape
        that Webull does (see `shared.crypto.kraken._normalize_kraken
        _order`), so the FILLED / REJECTED / EXPIRED branches below
        work uniformly for both lanes.

    Returns a counts dict for observability. Never raises — a broker
    outage or DB slowness cannot crash the auto-router tick.
    """
    counts = {
        "polled": 0,
        "filled": 0,
        "rejected_terminal": 0,
        "rejected_retry": 0,
        "no_change": 0,
        "errors": 0,
        "requeue_near_boundary": 0,
        "skipped_rate_limited": 0,
        "by_lane": {"equity": 0, "crypto": 0},
    }
    # Rate-limit: skip if a sweep ran within the last
    # RECONCILE_MIN_INTERVAL_SEC seconds. Protects Webull's per-second
    # `get_order` budget when `force_one_tick()` is called back-to-
    # back from intents.py on every intent insert.
    global _LAST_RECONCILE_SWEEP_TS
    now_utc = datetime.now(timezone.utc)
    if _LAST_RECONCILE_SWEEP_TS is not None:
        elapsed = (now_utc - _LAST_RECONCILE_SWEEP_TS).total_seconds()
        if elapsed < RECONCILE_MIN_INTERVAL_SEC:
            counts["skipped_rate_limited"] = 1
            return counts
    _LAST_RECONCILE_SWEEP_TS = now_utc

    try:
        # Local imports keep the module-level import graph clean and
        # avoid any circular pull at auto_router boot.
        from shared.broker_router import get_webull_adapter  # noqa: WPS433
        from shared.crypto.broker_adapter import get_kraken_adapter  # noqa: WPS433
        from shared.broker_error_taxonomy import classify  # noqa: WPS433
    except Exception as exc:  # noqa: BLE001
        logger.warning("reconcile sweep: adapter/classify import failed: %s", exc)
        return counts

    # Resolve each lane's adapter independently — an outage on one
    # broker must NEVER wedge the sweep for the other.
    adapters: dict[str, Any] = {}
    try:
        wb = await get_webull_adapter()
        if wb is not None:
            adapters["equity"] = wb
    except Exception as exc:  # noqa: BLE001
        logger.warning("reconcile sweep: get_webull_adapter failed: %s", exc)
    try:
        kr = await get_kraken_adapter()
        if kr is not None:
            adapters["crypto"] = kr
    except Exception as exc:  # noqa: BLE001
        logger.warning("reconcile sweep: get_kraken_adapter failed: %s", exc)

    if not adapters:
        return await _finish_sweep(counts)

    poll_cutoff = (now_utc - timedelta(seconds=RECONCILE_MIN_AGE_SEC)).isoformat()

    # Query intents PER LANE so the equity-Webull budget and the
    # crypto-Kraken budget are drained in separate batches.
    #
    # 2026-07-14 (iter-30): exclude intents whose broker_order id
    # starts with `mock-`. Those IDs are minted by `mc_pulse/
    # e2e_trace.py` for tracing (never a real broker order), but
    # if such a trace ever gets persisted as a `submitted` intent
    # the reconcile sweep keeps polling Webull `get_order(mock-*)`
    # forever — each call 429s, four in a row trips the Webull
    # circuit breaker, and the log floods. Filter here so mock
    # IDs are ignored at the source; the aging sweeper
    # (`_sweep_expired_unrouted`) will terminal them via the
    # standard 120min TTL path.
    pending_by_lane: dict[str, list[dict]] = {}
    for lane_name in adapters.keys():
        try:
            cur = (
                db[SHARED_INTENTS]
                .find(
                    {
                        "gate_state": "submitted",
                        "lane": lane_name,
                        # Webull stamps `broker_order.id`; Kraken stamps
                        # `broker_order.order_id`. Accept either — the
                        # per-intent extraction below reads both.
                        "$or": [
                            {"broker_order.id": {"$exists": True, "$ne": None}},
                            {"broker_order.order_id": {"$exists": True, "$ne": None}},
                        ],
                        # Reject synthetic mock IDs (see block comment).
                        "broker_order.id": {"$not": {"$regex": "^mock-"}},
                        "broker_order.order_id": {"$not": {"$regex": "^mock-"}},
                        "executed_at": {"$lt": poll_cutoff},
                    },
                    {
                        "_id": 0, "intent_id": 1, "symbol": 1, "action": 1,
                        "lane": 1, "stack": 1, "ingest_ts": 1, "executed_at": 1,
                        "broker_order": 1, "submit_retry_count": 1,
                        "final_notional_usd": 1, "sizing_provenance": 1,
                    },
                )
                .max_time_ms(3000)
                .limit(RECONCILE_BATCH_CAP)
            )
            rows: list[dict] = []
            async for d in cur:
                rows.append(d)
            pending_by_lane[lane_name] = rows
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "reconcile sweep: %s query failed: %s", lane_name, exc,
            )

    # Flatten to a single processing list, tagging lane on each row.
    pending_intents: list[tuple[str, dict]] = []
    for lane_name, rows in pending_by_lane.items():
        for d in rows:
            pending_intents.append((lane_name, d))

    for lane_name, intent in pending_intents:
        counts["polled"] += 1
        counts["by_lane"][lane_name] = counts["by_lane"].get(lane_name, 0) + 1
        adapter = adapters[lane_name]
        intent_id = intent.get("intent_id")
        bo_meta = intent.get("broker_order") or {}
        order_id = bo_meta.get("id") or bo_meta.get("order_id")
        if not intent_id or not order_id:
            counts["errors"] += 1
            continue

        try:
            bo = await asyncio.wait_for(
                adapter.get_order(str(order_id)),
                timeout=8.0,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "reconcile poll failed lane=%s intent=%s order_id=%s: %s",
                lane_name, intent_id, order_id, exc,
            )
            counts["errors"] += 1
            continue

        status = (bo.get("status") or "").upper()

        # ── FILLED ─────────────────────────────────────────────
        if status == "FILLED":
            try:
                await db[SHARED_INTENTS].update_one(
                    {"intent_id": intent_id},
                    {"$set": {
                        "gate_state": "filled",
                        "filled_at": _now_iso(),
                        "reconciled_by": AUTO_ROUTER_EMAIL,
                        "broker_order.status": "FILLED",
                        "broker_order.filled_qty": bo.get("filled_qty"),
                        "broker_order.filled_avg_price": bo.get("filled_avg_price"),
                        "broker_order.filled_at": bo.get("filled_at"),
                    }},
                )
                counts["filled"] += 1
                logger.info(
                    "reconcile FILLED intent=%s order_id=%s filled_qty=%s "
                    "avg_price=%s",
                    intent_id, order_id,
                    bo.get("filled_qty"), bo.get("filled_avg_price"),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "reconcile FILLED update failed intent=%s: %s",
                    intent_id, exc,
                )
                counts["errors"] += 1
            continue

        # ── REJECTED / CANCELLED / EXPIRED ─────────────────────
        if status in {"CANCELLED", "CANCELED", "REJECTED", "EXPIRED"}:
            reject_reason = str(
                bo.get("reject_reason")
                or bo.get("status_detail")
                or bo.get("last_error")
                or status
            )
            err = classify(reject_reason)
            retry_count = int(intent.get("submit_retry_count") or 0)

            if err.is_terminal or retry_count >= RECONCILE_MAX_RETRIES:
                # Release ledger reservation on terminal rejection.
                # Uses `final_notional_usd` stamped by the SUCCESS
                # path; falls back to `sizing_provenance.final_usd`.
                try:
                    from shared.capital.ledger import release_capital  # noqa: WPS433
                    lane_str = (intent.get("lane") or "").lower()
                    amount = float(
                        intent.get("final_notional_usd")
                        or (intent.get("sizing_provenance") or {}).get("final_usd")
                        or 0.0
                    )
                    if amount > 0 and lane_str in ("equity", "crypto"):
                        await release_capital(
                            lane=lane_str,
                            intent_id=intent_id,
                            amount=amount,
                            reason="broker_terminal_reject",
                        )
                except Exception:  # noqa: BLE001
                    logger.debug(
                        "reconcile: release_capital failed on terminal "
                        "reject intent=%s", intent_id,
                    )
                try:
                    await db[SHARED_INTENTS].update_one(
                        {"intent_id": intent_id},
                        {"$set": {
                            "gate_state": "broker_rejected",
                            "rejected_at": _now_iso(),
                            "reconciled_by": AUTO_ROUTER_EMAIL,
                            "broker_reason": err.bucket,
                            "broker_error_detail": err.detail,
                            "broker_error_terminal": bool(err.is_terminal),
                            "submit_retry_count": retry_count,
                        }},
                    )
                    counts["rejected_terminal"] += 1
                    logger.info(
                        "reconcile REJECTED (terminal) intent=%s bucket=%s "
                        "retries=%d/%d",
                        intent_id, err.bucket, retry_count,
                        RECONCILE_MAX_RETRIES,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "reconcile REJECTED-terminal update failed intent=%s: %s",
                        intent_id, exc,
                    )
                    counts["errors"] += 1
                continue

            # Transient reject under cap → requeue as `pending`.
            try:
                # Near-boundary check: if the intent is already >75%
                # of the way to lookback expiry, log a WARNING so the
                # operator can spot slow retry cycles that risk
                # stalling in the 60-120min zone before the expire
                # sweep catches them.
                age_min = _minutes_since_iso(intent.get("ingest_ts"), now_utc)
                near_boundary = (
                    age_min is not None
                    and age_min > RECONCILE_BOUNDARY_WARN_MIN
                )
                if near_boundary:
                    counts["requeue_near_boundary"] += 1
                    logger.warning(
                        "reconcile requeue NEAR BOUNDARY intent=%s "
                        "age_min=%.1f retry=%d/%d bucket=%s — approaching "
                        "60min lookback; expire-sweep backstop at 120min",
                        intent_id, age_min, retry_count + 1,
                        RECONCILE_MAX_RETRIES, err.bucket,
                    )

                await db[SHARED_INTENTS].update_one(
                    {"intent_id": intent_id},
                    {
                        "$set": {
                            "gate_state": "pending",
                            "executed": False,
                            "submit_retry_count": retry_count + 1,
                            "last_reject_at": _now_iso(),
                            "last_reject_bucket": err.bucket,
                            "last_reject_detail": err.detail,
                            "reconciled_by": AUTO_ROUTER_EMAIL,
                        },
                        "$unset": {
                            "broker_order": "",
                            "executed_at": "",
                            "executed_by": "",
                        },
                    },
                )
                counts["rejected_retry"] += 1
                logger.info(
                    "reconcile REJECTED (retry %d/%d) intent=%s bucket=%s "
                    "→ requeued as pending",
                    retry_count + 1, RECONCILE_MAX_RETRIES,
                    intent_id, err.bucket,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "reconcile REJECTED-retry update failed intent=%s: %s",
                    intent_id, exc,
                )
                counts["errors"] += 1
            continue

        # ── PARTIAL / WORKING / SUBMITTED / PENDING_CANCEL ─────
        # 2026-07-28 operator autopsy (LCID $6.88 limit vs $7.71
        # live): an unfilled entry order must NOT rest all day. A
        # momentum entry either fills within the TTL or the setup is
        # gone — a resting limit under a runner only fills when the
        # trade is already WRONG (pure adverse selection). Cancel at
        # the broker + terminal-expire the intent (never requeue: re-
        # chasing a runaway price is the same bad trade).
        age_min = _minutes_since_iso(
            intent.get("executed_at") or intent.get("ingest_ts"), now_utc,
        )
        if age_min is not None and age_min >= ENTRY_ORDER_TTL_MIN:
            filled_qty = 0.0
            try:
                filled_qty = float(bo.get("filled_qty") or 0.0)
            except (TypeError, ValueError):
                pass
            try:
                await asyncio.wait_for(
                    adapter.cancel_order(str(order_id)), timeout=8.0,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "entry-order TTL cancel FAILED intent=%s order=%s: %s",
                    intent_id, order_id, exc,
                )
                counts["errors"] += 1
                continue
            if filled_qty <= 0:
                # Nothing filled — release the full reservation.
                try:
                    from shared.capital.ledger import release_capital  # noqa: WPS433
                    lane_str = (intent.get("lane") or "").lower()
                    amount = float(
                        intent.get("final_notional_usd")
                        or (intent.get("sizing_provenance") or {}).get("final_usd")
                        or 0.0
                    )
                    if amount > 0 and lane_str in ("equity", "crypto"):
                        await release_capital(
                            lane=lane_str,
                            intent_id=intent_id,
                            amount=amount,
                            reason="entry_order_ttl_cancel",
                        )
                except Exception:  # noqa: BLE001
                    pass
            new_state = "filled" if filled_qty > 0 else "expired_unfilled"
            try:
                await db[SHARED_INTENTS].update_one(
                    {"intent_id": intent_id},
                    {"$set": {
                        "gate_state": new_state,
                        "expired_at": _now_iso(),
                        "reconciled_by": AUTO_ROUTER_EMAIL,
                        "broker_reason": (
                            f"entry_order_ttl_cancelled_after_{age_min:.0f}min"
                            + ("_partial_fill" if filled_qty > 0 else "")
                        ),
                        "broker_order.status": "CANCELLED_TTL",
                        "broker_order.filled_qty": filled_qty,
                    }},
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "entry-order TTL state update failed intent=%s: %s",
                    intent_id, exc,
                )
            counts["ttl_cancelled"] = counts.get("ttl_cancelled", 0) + 1
            logger.info(
                "entry order TTL CANCELLED intent=%s %s %s age=%.1fmin "
                "filled_qty=%s (partial positions are adopted by the "
                "exit monitor)",
                intent_id, lane_name, intent.get("symbol"),
                age_min, filled_qty,
            )
            continue

        # Younger than the TTL — leave alone and poll again next tick.
        counts["no_change"] += 1

    return await _finish_sweep(counts)


async def _finish_sweep(counts: dict) -> dict:
    """Common tail for `_sweep_submitted_broker_orders`. Handles:
      * The reconcile-sweep log line.
      * The learning-loop heartbeat resolver (iter-22 Stage 1.5).

    Extracted into a helper so the "no adapters available" early
    return still runs the resolver — otherwise a broker outage
    would silently starve the learning tape of outcome resolutions.
    """
    if counts["polled"]:
        logger.info(
            "auto_router reconcile sweep: polled=%d (equity=%d crypto=%d) "
            "filled=%d rejected_terminal=%d rejected_retry=%d no_change=%d "
            "errors=%d requeue_near_boundary=%d",
            counts["polled"],
            counts["by_lane"].get("equity", 0),
            counts["by_lane"].get("crypto", 0),
            counts["filled"],
            counts["rejected_terminal"], counts["rejected_retry"],
            counts["no_change"], counts["errors"],
            counts["requeue_near_boundary"],
        )

    # ── Learning-loop heartbeat resolver ─────────────────────────────
    # Piggybacks the reconcile tick's rate-limit window — no new
    # scheduler. Fills in `outcome_5m_bps` / `15m_bps` / `1h_bps` on
    # any ripe `learning_experiences` row. Crypto resolves via the
    # public Kraken ticker; equity is stubbed until Stage 2 wires
    # a real mark-price feed. Best-effort: any failure counted into
    # `learning_resolver_errors` but NEVER re-raised — the reconcile
    # tick must always return cleanly to the auto-router.
    try:
        from shared.learning.outcome_resolver import (  # noqa: WPS433
            resolve_pending_outcomes,
        )
        learn_counts = await asyncio.wait_for(
            resolve_pending_outcomes(db), timeout=8.0,
        )
        counts["learning_scanned"] = learn_counts.get("scanned", 0)
        counts["learning_resolved_5m"] = learn_counts.get("resolved_5m", 0)
        counts["learning_resolved_15m"] = learn_counts.get("resolved_15m", 0)
        counts["learning_resolved_1h"] = learn_counts.get("resolved_1h", 0)
        counts["learning_skipped_mark"] = learn_counts.get(
            "skipped_missing_mark", 0,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "auto_router reconcile sweep: learning resolver failed: %s", exc,
        )
        counts["learning_resolver_errors"] = 1

    # ── Counterfactual signal resolver (2026-02-19) ──────────────
    # Scores blocked-directional signals as market moves — same
    # cadence as the learning outcome resolver. Best-effort.
    try:
        from shared.counterfactuals import (  # noqa: WPS433
            resolve_pending_signals,
        )
        cf_counts = await asyncio.wait_for(
            resolve_pending_signals(db), timeout=8.0,
        )
        counts["counterfactual_scanned"] = cf_counts.get("scanned", 0)
        counts["counterfactual_resolved_5m"] = cf_counts.get("resolved_5m", 0)
        counts["counterfactual_resolved_15m"] = cf_counts.get("resolved_15m", 0)
        counts["counterfactual_resolved_1h"] = cf_counts.get("resolved_1h", 0)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "auto_router reconcile sweep: counterfactual resolver failed: %s",
            exc,
        )
        counts["counterfactual_resolver_errors"] = 1

    return counts


def _minutes_since_iso(iso_str: Optional[str], now_utc: datetime) -> Optional[float]:
    """Best-effort parse of an ISO-8601 UTC timestamp string → age in
    minutes from `now_utc`. Returns None on parse failure so callers
    can skip the near-boundary log without crashing."""
    if not iso_str:
        return None
    try:
        # fromisoformat handles the standard "+00:00" suffix; Python's
        # ISO parser is picky about the `Z` shorthand so normalize it.
        s = iso_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (now_utc - dt).total_seconds() / 60.0
    except (TypeError, ValueError):
        return None

