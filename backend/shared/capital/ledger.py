"""Per-Lane Capital Cap Ledger — atomic reservation store.

Doctrine pin (2026-02-20, operator-approved):
    Total capital cap needs atomic enforcement across concurrent
    executors. With one executor per lane, naive check-then-write
    against a shared counter allows both to pass a "room available"
    check before either commits. This module provides the atomic
    primitive: `find_one_and_update` with a conditional filter
    (`reserved <= total - amount`). Mongo guarantees the compare-
    and-swap; no application-level locking needed.

Design (from PRD 2026-02-20):
    * Two independent ledger docs — one per lane (equity, crypto).
      Never contend for the same reservation → zero cross-lane
      race by construction.
    * Each lane doc:
        {
          "_id": "<lane>_cap",
          "lane": "<lane>",
          "total": <float>,
          "reserved": <float>,
          "updated_at": <iso-string>,
          "reservations": [                # append-only audit trail
            {
              "intent_id": "<uuid>",
              "amount": <float>,
              "reserved_at": <iso-string>,
              "status": "open" | "released",
              "release_reason": Optional[str],   # only when released
              "released_at": Optional[iso-string],
            },
            ...
          ],
        }

    * Reservation amount source: `intent.notional` AFTER
      `sizing_gate.evaluate_sizing_with_ladder()` has clamped it —
      the intent already carries the correct value.

    * Route filter: only `live_micro` and `live_normal` intents
      call `reserve_capital`. `observe` / `paper` skip the ledger.
      The executor is responsible for reading
      `intent.sizing_provenance["route"]` and only invoking
      `reserve_capital` on the live branch.

    * Stale sweep: crashed executor mid-submit leaves "open"
      reservations that never get released. `sweep_stale_reservations`
      releases any older than the lane-specific `max_age_minutes`
      threshold. Run on a scheduled cadence (folded into an existing
      scheduler at wire-up time).

    * Read-only path (`get_lane_headroom`): safe for Tier-2 roles
      (Auditor, Governor, Strategist) — no writes, no race
      exposure. Frontend headroom tiles read this too.

Doctrine anti-patterns this module WILL NOT do:
    * Cross-lane reservation. Each `reserve_capital` call is
      scoped to ONE lane. Callers who want a global picture
      compose two `get_lane_headroom` calls in the read path.
    * Fresh `get_stage(brain, lane)` lookup at ledger time. The
      caller passes the resolved route (from
      `intent.sizing_provenance`) via the caller's route guard —
      the ledger itself is route-blind. Prevents ladder-promotion-
      mid-flight disagreements.
    * Silent failures. `reserve_capital` returns bool. Callers
      must decide REJECT vs QUEUE. `release_capital` is
      idempotent on unknown `intent_id` — logs a warning, does
      NOT raise.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from pymongo import ReturnDocument

from db import db
from namespaces import CAPITAL_LEDGER

logger = logging.getLogger(__name__)


VALID_LANES = ("equity", "crypto")
_ISO = lambda: datetime.now(timezone.utc).isoformat()  # noqa: E731


def _doc_id(lane: str) -> str:
    if lane not in VALID_LANES:
        raise ValueError(f"invalid lane: {lane!r} (must be one of {VALID_LANES})")
    return f"{lane}_cap"


# ─────────────────────────────── init ───────────────────────────────


async def init_ledger(equity_cap: float, crypto_cap: float) -> None:
    """Idempotent boot-time upsert of the two lane docs.

    * If a lane doc doesn't exist → creates it with `total=<cap>`
      and `reserved=0.0`.
    * If a lane doc DOES exist → leaves `reserved` alone (running
      reservations survive restarts). Only `total` is refreshed
      from the current env-supplied cap, in case the operator has
      raised the cap between boots.
    """
    for lane, cap in (("equity", equity_cap), ("crypto", crypto_cap)):
        cap_f = float(cap or 0.0)
        await db[CAPITAL_LEDGER].update_one(
            {"_id": _doc_id(lane)},
            {
                # Refresh `total` and `updated_at` on every boot so
                # operator raises take effect without a manual DB edit.
                # Do NOT touch `reserved` here — that's live state.
                "$set": {
                    "lane": lane,
                    "total": cap_f,
                    "updated_at": _ISO(),
                },
                "$setOnInsert": {
                    "reserved": 0.0,
                    "reservations": [],
                },
            },
            upsert=True,
        )
    logger.info(
        "capital_ledger init: equity_cap=%s crypto_cap=%s",
        equity_cap, crypto_cap,
    )


# ────────────────────────────── reserve ──────────────────────────────


async def reserve_capital(
    lane: str, amount: float, intent_id: str,
) -> bool:
    """Atomically reserve `amount` against `lane`.

    Returns True on success; False if the reservation would exceed
    `total`. Failure is not exceptional — the caller decides
    REJECT vs QUEUE.

    Idempotent on `intent_id` (2026-02-20 doctrine pin):
        If an OPEN reservation with the same `intent_id` already
        exists IN THIS LANE, this call is a no-op and returns True.
        This is the REAL scenario the operator flagged: a transient
        broker error in `_route_one` leaves the intent eligible for
        a next-tick retry WITHOUT releasing the reservation. When
        the retry re-enters `_route_one`, `reserve_capital` fires
        again with the same `intent_id`. Without idempotency, that
        would double-charge the ledger and eventually starve the
        cap over a few retry cycles.

        Idempotency is scoped PER LANE, not global — the CAS
        filter's `_id: <lane>_cap` restricts the write to a single
        lane doc, and the `$elemMatch` on `reservations` only
        scans that lane's own array. In practice this doesn't
        matter (any given intent has exactly one lane) but the
        invariant is that `(lane, intent_id)` is the composite
        idempotency key, not `intent_id` alone.

        The atomic CAS filter here requires BOTH:
          * `reserved <= total - amount` (cap headroom, as before)
          * NO open reservation with this `intent_id` (in this lane)
        A single Mongo document write evaluates both — a second
        caller for the same (lane, intent_id) finds the filter
        false because of the intent_id match, and we then
        distinguish that from "cap exceeded" via a follow-up read.

    Rejects zero/negative amounts (defensive — the sizing_gate
    should have clamped these already; ledger enforces the invariant
    on its own boundary).
    """
    doc_id = _doc_id(lane)
    if amount <= 0:
        logger.warning(
            "capital_ledger.reserve_capital: refused non-positive "
            "amount=%s lane=%s intent_id=%s", amount, lane, intent_id,
        )
        return False

    # First: guard against never-initialized ledger. `init_ledger`
    # should have run at boot; if not, refuse to reserve rather
    # than silently create a doc without a `total` cap.
    ledger_doc = await db[CAPITAL_LEDGER].find_one({"_id": doc_id})
    if ledger_doc is None:
        logger.error(
            "capital_ledger.reserve_capital: no ledger doc for lane=%s "
            "— init_ledger was not called at boot",
            lane,
        )
        return False

    total = float(ledger_doc.get("total") or 0.0)

    # Atomic CAS with idempotency: apply the $inc only if `reserved`
    # currently leaves enough headroom AND no open reservation with
    # this `intent_id` already exists. Two racing executors for the
    # SAME intent_id (retry-in-flight) will both hit the same filter;
    # exactly ONE lands the reservation, the other sees the filter
    # false and drops into the idempotent-return branch below.
    updated = await db[CAPITAL_LEDGER].find_one_and_update(
        {
            "_id": doc_id,
            "reserved": {"$lte": total - amount},
            "reservations": {
                "$not": {
                    "$elemMatch": {
                        "intent_id": intent_id,
                        "status": "open",
                    },
                },
            },
        },
        {
            "$inc": {"reserved": amount},
            "$set": {"updated_at": _ISO()},
            "$push": {
                "reservations": {
                    "intent_id": intent_id,
                    "amount": amount,
                    "reserved_at": _ISO(),
                    "status": "open",
                },
            },
        },
        return_document=ReturnDocument.AFTER,
    )
    if updated is not None:
        logger.info(
            "capital_ledger.reserve_capital: OK lane=%s amount=%s "
            "intent_id=%s (reserved=%.2f/%.2f)",
            lane, amount, intent_id,
            float(updated.get("reserved") or 0.0), total,
        )
        return True

    # Filter didn't match. Two possible reasons — distinguish them
    # so the caller can act correctly:
    #   (a) `intent_id` already has an open reservation → return
    #       True as a no-op (idempotent retry semantics).
    #   (b) not enough headroom → return False, caller blocks.
    existing = await db[CAPITAL_LEDGER].find_one(
        {
            "_id": doc_id,
            "reservations": {
                "$elemMatch": {
                    "intent_id": intent_id,
                    "status": "open",
                },
            },
        },
        {"_id": 1},
    )
    if existing is not None:
        # (a) idempotent: reservation already held under this
        # intent_id. Amount is whatever the FIRST call reserved
        # — do NOT bump it, do NOT bump `reserved`. This is the
        # retry-safe branch.
        logger.info(
            "capital_ledger.reserve_capital: IDEMPOTENT no-op lane=%s "
            "intent_id=%s — already has open reservation",
            lane, intent_id,
        )
        return True

    # (b) cap exceeded.
    logger.info(
        "capital_ledger.reserve_capital: REJECTED lane=%s amount=%s "
        "intent_id=%s (available=%.2f)",
        lane, amount, intent_id,
        total - float(ledger_doc.get("reserved") or 0.0),
    )
    return False


# ────────────────────────────── release ──────────────────────────────


async def release_capital(
    lane: str, intent_id: str, amount: float, reason: str,
) -> bool:
    """Release a prior reservation. Idempotent on unknown intent_id.

    Uses the positional `$` operator to update the matching
    reservation in-place — much cheaper than re-scanning the whole
    array. If the reservation is already released, this call is a
    no-op (idempotent — safe to retry from broker reconcile).

    `reason` classifiers (operator-observable):
        * "position_closed" — normal exit fill
        * "broker_terminal_reject" — order rejected non-transiently
        * "stale_timeout" — sweep_stale_reservations timeout
        * "cancelled" — operator-initiated cancel

    Returns True if a reservation was released; False if none was
    found in `open` status (idempotent retry / already released).
    """
    doc_id = _doc_id(lane)
    result = await db[CAPITAL_LEDGER].find_one_and_update(
        {
            "_id": doc_id,
            "reservations": {
                "$elemMatch": {
                    "intent_id": intent_id,
                    "status": "open",
                },
            },
        },
        {
            "$inc": {"reserved": -abs(float(amount))},
            "$set": {
                "updated_at": _ISO(),
                "reservations.$.status": "released",
                "reservations.$.release_reason": reason,
                "reservations.$.released_at": _ISO(),
            },
        },
        return_document=ReturnDocument.AFTER,
    )
    if result is None:
        logger.info(
            "capital_ledger.release_capital: no open reservation found "
            "lane=%s intent_id=%s (idempotent — no-op)",
            lane, intent_id,
        )
        return False
    logger.info(
        "capital_ledger.release_capital: OK lane=%s intent_id=%s "
        "amount=%s reason=%s",
        lane, intent_id, amount, reason,
    )
    return True


# ────────────────────── stale-reservation sweep ──────────────────────


async def sweep_stale_reservations(
    lane: str, max_age_minutes: int = 30,
) -> dict:
    """Release any `open` reservations older than `max_age_minutes`.

    Called on a scheduled cadence — a crashed executor mid-submit
    leaves the ledger holding capital that no live order is going
    to reconcile. This sweep frees it.

    Default 30 min is intentionally conservative — the sizing gate
    + executor path completes in seconds under normal load. Lane-
    specific tuning happens at scheduler wire-up time (equity fills
    fast; crypto may legitimately sit open longer).
    """
    doc_id = _doc_id(lane)
    cutoff_dt = datetime.now(timezone.utc) - timedelta(minutes=max_age_minutes)
    cutoff_iso = cutoff_dt.isoformat()

    doc = await db[CAPITAL_LEDGER].find_one({"_id": doc_id})
    if not doc:
        return {"lane": lane, "released": 0, "amount_released": 0.0}

    stale = [
        r for r in (doc.get("reservations") or [])
        if r.get("status") == "open"
        and (r.get("reserved_at") or "") < cutoff_iso
    ]
    total_released = 0.0
    for r in stale:
        released = await release_capital(
            lane=lane,
            intent_id=r.get("intent_id"),
            amount=float(r.get("amount") or 0.0),
            reason="stale_timeout",
        )
        if released:
            total_released += float(r.get("amount") or 0.0)

    if stale:
        logger.info(
            "capital_ledger.sweep_stale: lane=%s released=%s "
            "amount_released=%.2f max_age_minutes=%s",
            lane, len(stale), total_released, max_age_minutes,
        )
    return {
        "lane": lane,
        "released": len(stale),
        "amount_released": total_released,
    }


# ─────────────────────────── read (tier 2) ───────────────────────────


async def get_lane_headroom(lane: str) -> Optional[dict]:
    """Read-only lane headroom. Safe for Tier-2 roles (Auditor,
    Governor, Strategist) and frontend headroom tiles.

    Returns None if the ledger has not been initialised for this
    lane — caller decides whether to treat that as a warning
    (init_ledger not yet run at boot) or hard-fail.
    """
    doc_id = _doc_id(lane)
    doc = await db[CAPITAL_LEDGER].find_one(
        {"_id": doc_id},
        {"_id": 0, "total": 1, "reserved": 1, "updated_at": 1, "lane": 1},
    )
    if not doc:
        return None
    total = float(doc.get("total") or 0.0)
    reserved = float(doc.get("reserved") or 0.0)
    return {
        "lane": doc.get("lane") or lane,
        "total": total,
        "reserved": reserved,
        "available": total - reserved,
        "utilization_pct": (
            round(reserved / total * 100.0, 2) if total > 0 else 0.0
        ),
        "updated_at": doc.get("updated_at"),
    }


async def get_all_headroom() -> dict:
    """Composite read for dashboards — both lanes in one call."""
    return {
        lane: await get_lane_headroom(lane) for lane in VALID_LANES
    }


async def get_open_reservations(lane: str, limit: int = 50) -> list[dict]:
    """Return the currently-open reservations for a lane — for the
    Ops Room ledger tile. Newest first, capped at `limit`."""
    doc_id = _doc_id(lane)
    doc = await db[CAPITAL_LEDGER].find_one(
        {"_id": doc_id},
        {"_id": 0, "reservations": 1},
    )
    if not doc:
        return []
    open_res = [
        r for r in (doc.get("reservations") or [])
        if r.get("status") == "open"
    ]
    open_res.sort(key=lambda r: r.get("reserved_at") or "", reverse=True)
    return open_res[:limit]
