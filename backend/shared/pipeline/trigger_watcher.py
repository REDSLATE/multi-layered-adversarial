"""Paradox v3 — Trigger Watcher (DORMANT by default).

Step 3 of the v3 rollout (PRD §7). Ships the rails for the v3
WAIT_FOR_TRIGGER lifecycle but does NOT activate until the operator
sets `PARADOX_V3_TRIGGER_WATCHER=1`. Until then, every public entry
point in this module is a no-op.

Lifecycle (when live, Step 5):
    seat_policy sees plan.intent == WAIT_FOR_TRIGGER
      → enqueue_watch_plan() stamps the queue row, sets gate_state
        on the intent doc to `waiting_for_trigger`.
    scan_watch_queue() ticks every ~5s:
      → for each watching row:
          * trigger_price hit:        gate_state → trigger_fired,
                                      re-inject into seat layer
          * invalidation_price hit:   gate_state → plan_invalidated
          * ttl elapsed:              gate_state → plan_expired
      → respective queue row state transitions, resolved_at stamped.

Doctrine pins:
  * READ-ONLY when the env flag is off. `scan_watch_queue()` returns
    a zero counter dict without touching Mongo.
  * The seat-layer integration (Step 5) is NOT wired in this module —
    seat_policy.py still has no awareness of v3 today. This module
    ships the helpers so seat_policy can call them when Step 5 lands.
  * Datetimes use `datetime.now(timezone.utc)` and are stored as BSON
    Date (not ISO string) — TTL indexes require BSON Date or they
    silently no-op.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from db import db
from namespaces import SHARED_INTENTS

_log = logging.getLogger("risedual.paradox_v3.trigger_watcher")

# Collection name. The Mongo TTL index lives on the `queued_at` field
# with a 30-day retention so orphan rows can never accumulate even if
# the watcher loop misses a tick. See db.py boot-time index creation.
INTENT_WATCH_QUEUE_COLL = "intent_watch_queue"

# When live, the watcher MUST never let a WAIT plan sit forever. The
# TTL-on-queue safety net is 30 days; the per-plan ttl_seconds caps
# it further. This constant is the safety-net default — every v3 plan
# is expected to ship its own ttl_seconds (or have it derived from
# horizon at the seat layer).
SAFETY_TTL_SECONDS = 30 * 86_400  # 30 days


# ── Feature flag ────────────────────────────────────────────────────
def is_watcher_enabled(env_var: str = "PARADOX_V3_TRIGGER_WATCHER") -> bool:
    """True iff the operator has opted INTO trigger-watching.

    Source of truth, in order:
      1. `system_flags.trigger_watcher_enabled` (DB-backed, flippable
         from the admin UI without restart — 2026-02-23 operator fix)
      2. env var fallback

    Default OFF on both empty. Pinned in two tests
    (`test_trigger_watcher_dormant_*`) so a future env-cleanup pass
    can't quietly flip the default.
    """
    try:
        from shared.system_flags import get_system_flags
        snap = get_system_flags()
        if snap.trigger_watcher_enabled is not None:
            return bool(snap.trigger_watcher_enabled)
    except Exception:  # noqa: BLE001
        pass
    val = os.environ.get(env_var, "0").strip().lower()
    return val in {"1", "true", "yes", "on"}


def is_refire_enabled(env_var: str = "PARADOX_V3_TRIGGER_REFIRE") -> bool:
    """True iff the operator has opted INTO live re-firing of fired
    plans through the unified pipeline.

    Source of truth, in order:
      1. `system_flags.trigger_refire_enabled` (DB-backed, flippable
         from the admin UI without restart — 2026-02-23 operator fix)
      2. env var fallback

    Default OFF — the operator may want to run the watcher in
    observability-only mode first (Step 5 ship) before letting trigger
    fires translate into actual broker calls. This second flag exists
    so the activation order is: (1) flip watcher on, watch the queue
    drain TTL'd rows; (2) flip refire on, watch a fired plan actually
    reach the broker.
    """
    try:
        from shared.system_flags import get_system_flags
        snap = get_system_flags()
        if snap.trigger_refire_enabled is not None:
            return bool(snap.trigger_refire_enabled)
    except Exception:  # noqa: BLE001
        pass
    val = os.environ.get(env_var, "0").strip().lower()
    return val in {"1", "true", "yes", "on"}


# ── Enqueue helper (called from seat_policy in Step 5) ─────────────
async def enqueue_watch_plan(
    *,
    intent_id: str,
    symbol: str,
    lane: str,
    stance: str,
    trigger_price: Optional[float],
    invalidation_price: Optional[float],
    expires_at: Optional[datetime],
) -> Dict[str, Any]:
    """Park a v3 WAIT_FOR_TRIGGER plan on the watch queue.

    Seat policy calls this in Step 5 when a v3 intent arrives with
    `plan.intent == 'WAIT_FOR_TRIGGER'`. The plan is removed from the
    main pipeline (gate_state → `waiting_for_trigger` on the intent
    doc) and parked here until `scan_watch_queue()` fires it.

    DORMANT semantics: when the env flag is off, the row is still
    written so the operator can later flip the flag and process a
    backlog. The watcher just doesn't transition any rows.
    """
    now = datetime.now(timezone.utc)
    row = {
        "intent_id": intent_id,
        "symbol": symbol,
        "lane": lane,
        "stance": stance,
        "trigger_price": (
            float(trigger_price) if trigger_price is not None else None
        ),
        "invalidation_price": (
            float(invalidation_price) if invalidation_price is not None else None
        ),
        "queued_at": now,
        "expires_at": expires_at,
        "state": "watching",
        "resolved_at": None,
        "resolved_reason": None,
    }
    try:
        await db[INTENT_WATCH_QUEUE_COLL].insert_one(row.copy())
        # Stamp the intent doc so the funnel + post-mortem see a
        # canonical terminal-ish state (WAIT rows naturally land at
        # Stage 1 — see funnel route).
        await db[SHARED_INTENTS].update_one(
            {"intent_id": intent_id},
            {"$set": {"gate_state": "waiting_for_trigger"}},
        )
    except Exception as exc:  # noqa: BLE001
        _log.warning(
            "enqueue_watch_plan failed intent=%s sym=%s err=%s",
            intent_id, symbol, exc,
        )
    return row


# ── Periodic worker ────────────────────────────────────────────────
async def scan_watch_queue(
    *,
    now: Optional[datetime] = None,
    price_fetcher=None,
) -> Dict[str, Any]:
    """Single tick of the trigger watcher.

    DORMANT-mode behaviour (default):
      Returns `{"enabled": False, ...}` zero-counters. NO Mongo
      reads, NO writes, NO broker calls.

    LIVE-mode behaviour (when `PARADOX_V3_TRIGGER_WATCHER=1`):
      Step 3 ships the TTL-expiry leg only. Price-trigger and
      invalidation-trigger paths are Step 5 work — they would
      require a snapshot fetcher (the optional `price_fetcher`
      callable). When `price_fetcher` is None even in live mode,
      this function only expires TTL'd rows; price triggers stay
      untouched. This is the same defensive pattern as the
      auto-router — a flag flip lets the operator inspect the queue
      drain before wiring price triggers.

    Args:
        now: optional override for time-now (test injection).
        price_fetcher: optional async callable
            `(symbol, lane) -> dict[bid/ask/price]`. If None, only
            TTL-expiry processing runs.

    Returns:
        Counter dict with `enabled`, `scanned`, `fired`,
        `invalidated`, `expired` keys.
    """
    enabled = is_watcher_enabled()
    counters = {
        "enabled":     enabled,
        "scanned":     0,
        "fired":       0,
        "invalidated": 0,
        "expired":     0,
    }
    if not enabled:
        return counters

    now = now or datetime.now(timezone.utc)
    cursor = db[INTENT_WATCH_QUEUE_COLL].find({"state": "watching"})
    async for row in cursor:
        counters["scanned"] += 1
        expires_at = row.get("expires_at")
        if isinstance(expires_at, datetime):
            # Ensure tz-aware comparison — Mongo can sometimes return
            # naive datetimes depending on motor version.
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if now >= expires_at:
                await _mark_resolved(
                    row, state="expired", reason="ttl_elapsed", now=now,
                    intent_gate_state="plan_expired",
                )
                counters["expired"] += 1
                continue

        # Price-trigger + invalidation legs — only run when caller
        # supplies a fetcher. Step 5 wires the default fetcher; until
        # then this code path is silent.
        if price_fetcher is not None:
            try:
                snap = await price_fetcher(row["symbol"], row.get("lane"))
            except Exception as exc:  # noqa: BLE001
                _log.warning(
                    "price_fetcher failed sym=%s err=%s",
                    row.get("symbol"), exc,
                )
                snap = None
            if not snap:
                continue
            outcome = _classify_trigger(row, snap)
            if outcome == "fired":
                await _mark_resolved(
                    row, state="fired", reason="trigger_price_hit", now=now,
                    intent_gate_state="trigger_fired",
                )
                counters["fired"] += 1
                # ─── Re-injection (Step 5.b) — opt-in via env flag ──
                # When `PARADOX_V3_TRIGGER_REFIRE=1` is set, a fired
                # plan is re-run through the unified pipeline with
                # `execution.action` synthesised from `plan.stance`.
                # The seat re-evaluates conf_min + consensus AT
                # trigger-fire time (not at park time) so a plan that
                # waited 6h doesn't ride stale doctrine. When the
                # flag is OFF (default), trigger fire is
                # observability-only — `gate_state=trigger_fired`
                # gets stamped but no broker call is attempted.
                if is_refire_enabled():
                    try:
                        await _refire_trigger_fired_plan(row, now=now)
                    except Exception as rexc:  # noqa: BLE001
                        _log.warning(
                            "trigger_watcher refire failed intent=%s err=%s",
                            row.get("intent_id"), rexc,
                        )
            elif outcome == "invalidated":
                await _mark_resolved(
                    row, state="invalidated", reason="invalidation_price_hit",
                    now=now, intent_gate_state="plan_invalidated",
                )
                counters["invalidated"] += 1

    return counters


def _classify_trigger(row: Dict[str, Any], snap: Dict[str, Any]) -> Optional[str]:
    """Return `"fired" | "invalidated" | None` based on snapshot price.

    Side-aware: BULLISH plans fire UP through trigger_price and
    invalidate DOWN through invalidation_price. BEARISH plans are
    the mirror.
    """
    price = snap.get("price") or snap.get("last")
    if price is None:
        return None
    try:
        price = float(price)
    except (TypeError, ValueError):
        return None

    stance = (row.get("stance") or "").upper()
    is_bullish = stance in {"BULLISH", "LONG_BIAS"}
    is_bearish = stance in {"BEARISH", "SHORT_BIAS"}
    trig = row.get("trigger_price")
    inv = row.get("invalidation_price")

    if is_bullish:
        if inv is not None and price <= float(inv):
            return "invalidated"
        if trig is not None and price >= float(trig):
            return "fired"
    elif is_bearish:
        if inv is not None and price >= float(inv):
            return "invalidated"
        if trig is not None and price <= float(trig):
            return "fired"
    return None


async def _mark_resolved(
    row: Dict[str, Any], *, state: str, reason: str,
    now: datetime, intent_gate_state: str,
) -> None:
    """Transition a watching row to a terminal state + stamp the
    intent doc's gate_state."""
    try:
        await db[INTENT_WATCH_QUEUE_COLL].update_one(
            {"_id": row["_id"]},
            {"$set": {
                "state":           state,
                "resolved_at":     now,
                "resolved_reason": reason,
            }},
        )
        await db[SHARED_INTENTS].update_one(
            {"intent_id": row["intent_id"]},
            {"$set": {"gate_state": intent_gate_state}},
        )
    except Exception as exc:  # noqa: BLE001
        _log.warning(
            "trigger_watcher mark_resolved failed intent=%s state=%s err=%s",
            row.get("intent_id"), state, exc,
        )


# ── Re-injection of fired plans (Step 5.b) ──────────────────────────
_BULLISH_STANCES = {"BULLISH", "LONG_BIAS"}
_BEARISH_STANCES = {"BEARISH", "SHORT_BIAS"}


def _derive_action_from_stance(stance: str, lane: str) -> Optional[str]:
    """Map `plan.stance` + lane → legacy execution action for re-injection.

    Doctrine (operator pin 2026-02-22 — corrected from initial draft):
      * BULLISH / LONG_BIAS → "BUY" on both lanes.
      * BEARISH / SHORT_BIAS → "SHORT" on both lanes. Kraken supports
        margin shorts via leverage; Public.com / Webull support
        equity shorts. The broker's own caps (leverage limits, locate
        availability, margin available) gate the actual fill
        downstream — not the watcher.
      * NEUTRAL / UNCERTAIN → None. A NEUTRAL plan that fires its
        trigger is logically inconsistent.
    """
    stance = (stance or "").upper()
    if stance in _BULLISH_STANCES:
        return "BUY"
    if stance in _BEARISH_STANCES:
        return "SHORT"
    return None


# Execution styles that imply "use a limit price" on the broker call.
# MARKET_NOW goes to market; everything else uses the trigger price
# as the limit (TRIGGERED_LIMIT is the canonical case — fire at-or-
# better than the trigger price the brain identified).
_LIMIT_STYLES = {"LIMIT", "TRIGGERED_LIMIT", "STOP", "PATIENT", "SCALED"}


def _derive_execution_pricing(
    plan: Dict[str, Any], row: Dict[str, Any],
) -> Dict[str, Optional[float]]:
    """Return `{"limit_price": float | None}` for the refire mutation.

    Source of truth for the limit price is `plan.trigger_price` (the
    level the brain identified as the entry). When
    `plan.execution_style == "MARKET_NOW"` we explicitly want
    `limit_price=None` so the broker routes through its market path.
    """
    style = (plan.get("execution_style") or "MARKET_NOW").upper()
    if style not in _LIMIT_STYLES:
        return {"limit_price": None}
    trig = plan.get("trigger_price")
    if trig is None:
        trig = row.get("trigger_price")
    if trig is None:
        return {"limit_price": None}
    try:
        return {"limit_price": float(trig)}
    except (TypeError, ValueError):
        return {"limit_price": None}


async def _refire_trigger_fired_plan(
    row: Dict[str, Any], *, now: datetime,
) -> Optional[Dict[str, Any]]:
    """Re-run a fired WAIT plan through the unified pipeline with
    `execution.action` synthesised from `plan.stance`.

    Returns the pipeline's verdict dict, or None if the plan was
    skipped (unparseable stance, missing intent doc, etc.).

    Doctrine pins:
      * The intent doc is mutated IN PLACE in Mongo before re-running
        so any downstream consumer reading the doc post-fire sees the
        new shape (`plan.intent="ENTER"`, `execution.action`
        populated).
      * `plan.intent` flips from `WAIT_FOR_TRIGGER` to `ENTER` so the
        seat's WAIT short-circuit doesn't re-park the plan in an
        infinite loop.
      * The seat re-evaluates `conf_min` and live consensus AT
        trigger-fire time. A plan that waited 6h with stale
        confidence rides the LIVE doctrine, not the parked one.
      * Failure is fail-soft — the queue row stays `fired` even if
        re-injection fails; the operator can investigate via the
        watch-queue snapshot.
    """
    intent_id = row.get("intent_id")
    if not intent_id:
        return None

    intent = await db[SHARED_INTENTS].find_one(
        {"intent_id": intent_id}, {"_id": 0},
    )
    if not intent:
        _log.warning(
            "trigger_watcher refire skipped — intent %s not in shared_intents",
            intent_id,
        )
        return None

    derived_action = _derive_action_from_stance(
        row.get("stance") or "", intent.get("lane") or "",
    )
    if derived_action is None:
        _log.info(
            "trigger_watcher refire skipped intent=%s stance=%s lane=%s "
            "(no derivable action — see _derive_action_from_stance docs)",
            intent_id, row.get("stance"), intent.get("lane"),
        )
        return None

    # Mutate the intent: flip plan.intent to ENTER + stamp execution.
    plan = dict(intent.get("plan") or {})
    plan["intent"] = "ENTER"
    pricing = _derive_execution_pricing(plan, row)
    execution = dict(intent.get("execution") or {})
    execution["action"] = derived_action
    execution["derived_at"] = now.isoformat()
    execution["derived_from_plan"] = True
    # 2026-02-22 (operator pin) — honour `plan.execution_style`. When
    # the brain called for a limit-class fill, stamp `limit_price` so
    # the broker routes through the limit path (Kraken supports both
    # market + limit; ditto Public.com / Webull). MARKET_NOW leaves
    # limit_price=None.
    execution["limit_price"] = pricing["limit_price"]

    intent["plan"] = plan
    intent["execution"] = execution
    intent["action"] = derived_action  # legacy v2 surface

    await db[SHARED_INTENTS].update_one(
        {"intent_id": intent_id},
        {"$set": {
            "plan": plan,
            "execution": execution,
            "action": derived_action,
            "gate_state": "trigger_fired_pending_execution",
        }},
    )

    # Defer import to avoid module-init cycles (adapter imports
    # pipeline which imports this module).
    from shared.pipeline.adapter import run_unified_for_intent
    notional = float(intent.get("notional_usd") or 0.0) or 10.0
    return await run_unified_for_intent(intent, notional)


# ── Default price fetcher (Step 5) ──────────────────────────────────
async def default_price_fetcher(symbol: str, lane: Optional[str]) -> Optional[Dict[str, float]]:
    """Reference price fetcher wired into the auto-router tick.

    Walks the existing `enrich_snapshot_spread` fallback ladder
    (brain → derive(bid/ask) → indicator cache → Kraken public →
    sentinel). Returns `{"price": float}` on success or None when
    no source produced a usable quote.

    Doctrine pin: this fetcher is FAIL-SOFT. Any error inside the
    ladder is swallowed and the watcher leaves the queue row in
    `watching` state — better to delay a trigger by one tick than to
    fire incorrectly on a stale quote.
    """
    try:
        from shared.market_data import enrich_snapshot_spread
        enriched, _diag = await enrich_snapshot_spread(
            {}, symbol=symbol, lane=lane,
        )
    except Exception as exc:  # noqa: BLE001
        _log.debug("default_price_fetcher enrich failed sym=%s err=%s", symbol, exc)
        return None
    # Prefer an explicit price/mid/last; otherwise derive from
    # bid+ask. Pull the value out defensively (some keys are strings
    # from upstream callers).
    for key in ("price", "mid", "last"):
        val = enriched.get(key)
        if val is None:
            continue
        try:
            return {"price": float(val)}
        except (TypeError, ValueError):
            continue
    bid, ask = enriched.get("bid"), enriched.get("ask")
    if bid is not None and ask is not None:
        try:
            return {"price": (float(bid) + float(ask)) / 2.0}
        except (TypeError, ValueError):
            return None
    return None


# ── Observability helper (read-only) ────────────────────────────────
async def watch_queue_snapshot(limit: int = 100) -> Dict[str, Any]:
    """Quick read of the current queue for the operator. Read-only.

    Returns counts by state + the most-recent N rows. Safe to call
    when the watcher is dormant — purely reads.
    """
    states = ("watching", "fired", "invalidated", "expired")
    counts: Dict[str, int] = {s: 0 for s in states}
    try:
        for s in states:
            counts[s] = await db[INTENT_WATCH_QUEUE_COLL].count_documents({"state": s})
        recent = await db[INTENT_WATCH_QUEUE_COLL].find(
            {}, {"_id": 0},
        ).sort("queued_at", -1).to_list(length=int(limit))
    except Exception as exc:  # noqa: BLE001
        _log.warning("watch_queue_snapshot failed: %s", exc)
        recent = []
    return {
        "enabled":   is_watcher_enabled(),
        "counts":    counts,
        "recent":    recent,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


__all__ = (
    "INTENT_WATCH_QUEUE_COLL",
    "SAFETY_TTL_SECONDS",
    "is_watcher_enabled",
    "is_refire_enabled",
    "enqueue_watch_plan",
    "scan_watch_queue",
    "default_price_fetcher",
    "watch_queue_snapshot",
)