"""Tests for the broker-status classifier — pins the canonical 5-
bucket lifecycle taxonomy across every status string we've seen
from Webull / Alpaca / Kraken / Public adapters.

Anyone adding a new broker MUST extend `_STATUS_MAP` in
`shared/broker_status_classifier.py` and add a row here.
"""
from __future__ import annotations

import pytest

from shared.broker_status_classifier import (
    ALL_BUCKETS,
    BUCKET_CANCELED,
    BUCKET_FILLED,
    BUCKET_PARTIALLY_FILLED,
    BUCKET_UNKNOWN,
    BUCKET_WORKING,
    classify_broker_status,
    empty_bucket_counts,
)


@pytest.mark.parametrize("status,expected", [
    # ── Filled ──
    ("FILLED", BUCKET_FILLED),
    ("filled", BUCKET_FILLED),                # case tolerance
    ("  FILLED  ", BUCKET_FILLED),            # whitespace
    ("CLOSED", BUCKET_FILLED),                # Kraken terminal-fill
    ("COMPLETE", BUCKET_FILLED),
    ("COMPLETED", BUCKET_FILLED),
    ("EXECUTED", BUCKET_FILLED),
    # ── Partially filled ──
    ("PARTIALLY_FILLED", BUCKET_PARTIALLY_FILLED),
    ("PARTIAL_FILL", BUCKET_PARTIALLY_FILLED),
    ("PARTIAL", BUCKET_PARTIALLY_FILLED),
    # ── Canceled ──
    ("CANCELED", BUCKET_CANCELED),
    ("CANCELLED", BUCKET_CANCELED),
    ("REJECTED", BUCKET_CANCELED),
    ("EXPIRED", BUCKET_CANCELED),
    ("FAILED", BUCKET_CANCELED),
    # ── Working ──
    ("WORKING", BUCKET_WORKING),
    ("OPEN", BUCKET_WORKING),
    ("NEW", BUCKET_WORKING),
    ("ACCEPTED", BUCKET_WORKING),
    ("PENDING", BUCKET_WORKING),
    ("PENDING_NEW", BUCKET_WORKING),
    ("SUBMITTED", BUCKET_WORKING),
    ("ACTIVE", BUCKET_WORKING),
    ("QUEUED", BUCKET_WORKING),
    # ── Unknown / fall-through ──
    ("", BUCKET_UNKNOWN),
    (None, BUCKET_UNKNOWN),
    ("MARGIN_REQUIRED", BUCKET_UNKNOWN),      # an unknown adapter code
    ("foobar", BUCKET_UNKNOWN),
])
def test_classify_status_string_only(status, expected):
    assert classify_broker_status(status) == expected


def test_filled_with_partial_qty_downgrades_to_partial():
    """Some adapters (Webull) report 'FILLED' even when the fill is
    partial. The qty tie-breaker MUST downgrade to PARTIALLY_FILLED."""
    out = classify_broker_status(
        "FILLED", filled_qty=3.0, ordered_qty=10.0,
    )
    assert out == BUCKET_PARTIALLY_FILLED


def test_filled_with_full_qty_stays_filled():
    out = classify_broker_status(
        "FILLED", filled_qty=10.0, ordered_qty=10.0,
    )
    assert out == BUCKET_FILLED


def test_empty_status_with_partial_qty_is_partial():
    """If the broker dropped the status but updated fill_qty, the
    qty path classifies the row as PARTIALLY_FILLED (defensive)."""
    out = classify_broker_status(
        None, filled_qty=2.0, ordered_qty=10.0,
    )
    assert out == BUCKET_PARTIALLY_FILLED


def test_empty_status_with_full_qty_is_filled():
    out = classify_broker_status(
        None, filled_qty=5.0, ordered_qty=5.0,
    )
    assert out == BUCKET_FILLED


def test_empty_status_with_some_fill_no_total_is_partial():
    """`filled_qty > 0` but no `ordered_qty` — best signal is partial."""
    out = classify_broker_status(None, filled_qty=3.0)
    assert out == BUCKET_PARTIALLY_FILLED


def test_empty_status_zero_fill_is_unknown():
    out = classify_broker_status(None, filled_qty=0.0, ordered_qty=10.0)
    assert out == BUCKET_UNKNOWN


def test_empty_bucket_counts_shape():
    """Every public bucket must appear in the zero-init dict."""
    z = empty_bucket_counts()
    assert set(z.keys()) == set(ALL_BUCKETS)
    assert all(v == 0 for v in z.values())


def test_invalid_filled_qty_doesnt_crash():
    """Defensive: stray string values from broker JSON don't crash
    the classifier — they fall through to status-only behavior."""
    # filled_qty is a string that can't float — should NOT raise.
    out = classify_broker_status("FILLED", filled_qty="N/A", ordered_qty=10.0)
    # Falls through cleanly to FILLED.
    assert out == BUCKET_FILLED
