"""Broker order status classifier — canonical 5-bucket lifecycle taxonomy.

Doctrine: After an intent reaches `executed=True` at MC (broker
accepted the order), the operator's next blind spot is "what
happened to the order at the broker?" Five canonical outcomes:

  * filled            — fully filled at broker (terminal, profitable lifecycle)
  * partially_filled  — partial fill, may still be working or terminal
  * canceled          — order canceled / rejected before fill (terminal, no PnL)
  * working           — accepted, no fill yet, still alive on the book
  * unknown           — no broker receipt available / status doesn't map

Different brokers use slightly different status strings:
  * Webull   : "FILLED", "PARTIALLY_FILLED", "WORKING", "CANCELED",
               "REJECTED", "PENDING", "SUBMITTED", "OPEN"
  * Alpaca   : "filled", "partially_filled", "canceled", "rejected",
               "new", "accepted", "pending_new"
  * Kraken   : "closed" (filled), "open" (working), "canceled",
               "expired"

The `classify_broker_status()` helper normalizes all of these into
the 5 canonical buckets so the dashboards / funnel tile / audit
trails can speak one taxonomy.
"""
from __future__ import annotations

from typing import Optional


# ── Canonical lifecycle buckets ──────────────────────────────────────
BUCKET_FILLED            = "filled"
BUCKET_PARTIALLY_FILLED  = "partially_filled"
BUCKET_CANCELED          = "canceled"
BUCKET_WORKING           = "working"
BUCKET_UNKNOWN           = "unknown"

ALL_BUCKETS: tuple[str, ...] = (
    BUCKET_FILLED,
    BUCKET_PARTIALLY_FILLED,
    BUCKET_WORKING,
    BUCKET_CANCELED,
    BUCKET_UNKNOWN,
)

# ── Status-string → bucket map ───────────────────────────────────────
# Uppercased, whitespace-trimmed lookups. Keep this as a single
# authoritative table — adding a new broker = one line per status.
_STATUS_MAP: dict[str, str] = {
    # ── Filled ──
    "FILLED":           BUCKET_FILLED,
    "CLOSED":           BUCKET_FILLED,         # Kraken terminal-fill
    "COMPLETE":         BUCKET_FILLED,         # legacy / generic
    "COMPLETED":        BUCKET_FILLED,
    "EXECUTED":         BUCKET_FILLED,
    # ── Partially filled ──
    "PARTIALLY_FILLED": BUCKET_PARTIALLY_FILLED,
    "PARTIAL_FILL":     BUCKET_PARTIALLY_FILLED,
    "PARTIAL":          BUCKET_PARTIALLY_FILLED,
    # ── Canceled / rejected ──
    "CANCELED":         BUCKET_CANCELED,
    "CANCELLED":        BUCKET_CANCELED,       # UK spelling
    "REJECTED":         BUCKET_CANCELED,
    "EXPIRED":          BUCKET_CANCELED,       # Kraken time-in-force lapse
    "FAILED":           BUCKET_CANCELED,
    # ── Working (accepted but not yet filled) ──
    "WORKING":          BUCKET_WORKING,
    "OPEN":             BUCKET_WORKING,        # Kraken active
    "NEW":              BUCKET_WORKING,        # Alpaca / FIX
    "ACCEPTED":         BUCKET_WORKING,
    "PENDING":          BUCKET_WORKING,
    "PENDING_NEW":      BUCKET_WORKING,        # Alpaca pre-accept
    "SUBMITTED":        BUCKET_WORKING,        # Webull submit-ack
    "ACTIVE":           BUCKET_WORKING,
    "QUEUED":           BUCKET_WORKING,
}


def classify_broker_status(
    status: Optional[str],
    *,
    filled_qty: Optional[float] = None,
    ordered_qty: Optional[float] = None,
) -> str:
    """Map a broker status string (+ optional fill quantities) to one
    of the 5 canonical lifecycle buckets.

    Rules:
      1. Explicit status-string match wins (table-driven).
      2. If status doesn't match but `filled_qty > 0 < ordered_qty`,
         classify as PARTIALLY_FILLED (defensive — some adapters drop
         the status string when only an updated fill_qty arrives).
      3. If status is missing/unrecognized and no fill info, return
         UNKNOWN — surfaces the gap to the operator instead of guessing.
    """
    if status:
        key = status.upper().strip()
        bucket = _STATUS_MAP.get(key)
        if bucket is not None:
            # Refinement: a "FILLED" status with filled_qty < ordered_qty
            # is actually a partial fill (rare, but Webull does this).
            if bucket == BUCKET_FILLED and filled_qty is not None and ordered_qty:
                try:
                    if 0 < float(filled_qty) < float(ordered_qty):
                        return BUCKET_PARTIALLY_FILLED
                except (TypeError, ValueError):
                    pass
            return bucket

    # Status-string fallthrough — use fill_qty as a tie-breaker.
    if filled_qty is not None:
        try:
            fq = float(filled_qty)
        except (TypeError, ValueError):
            fq = 0.0
        if fq > 0:
            if ordered_qty:
                try:
                    oq = float(ordered_qty)
                    if fq < oq:
                        return BUCKET_PARTIALLY_FILLED
                    return BUCKET_FILLED
                except (TypeError, ValueError):
                    pass
            return BUCKET_PARTIALLY_FILLED  # some fill, no total → partial

    return BUCKET_UNKNOWN


def empty_bucket_counts() -> dict[str, int]:
    """Initialize a zero-count dict for every canonical bucket — used
    by aggregators so the response shape is stable even on empty
    windows."""
    return {b: 0 for b in ALL_BUCKETS}
