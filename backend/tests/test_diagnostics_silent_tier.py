"""Tripwire — Diagnostics `effective_tier` must downgrade a
fresh-heartbeat brain to `silent` when its last decision receipt is
stale. This is the May-14 wrapper-hang surface: heartbeat loop keeps
pinging (dumb endpoint) while the intent/decision loop wedges on a
half-open socket. Without this tripwire, the operator UI lies for
days (the 2026-06 prod incident: BARRACUDA/HELLCAT/GTO heartbeating
fresh but silent for 6/7/12 days).
"""
from __future__ import annotations

import pytest

from shared.diagnostics import _effective_tier, _heartbeat_tier
from namespaces import RECEIPT_STALE_AFTER_SECONDS


# ── _heartbeat_tier — pure age math, no change in this pass.
def test_heartbeat_tier_bands():
    assert _heartbeat_tier(None) == "unknown"
    assert _heartbeat_tier(30) == "ok"
    assert _heartbeat_tier(150) == "stale"  # ≥120, <300
    assert _heartbeat_tier(900) == "dead"   # ≥300


# ── _effective_tier — the new join.
def test_effective_silent_when_heartbeat_fresh_but_receipts_stale():
    """The 2026-06 BARRACUDA pattern: heartbeat OK, receipt 6 days old."""
    receipt_age = float(60 * 60 * 24 * 6)  # 6 days
    assert _effective_tier("ok", receipt_age) == "silent"


def test_effective_silent_when_no_receipt_ever():
    """Brand-new pod that's heartbeating but never produced a decision —
    indistinguishable from a wedged loop, so we surface it as silent."""
    assert _effective_tier("ok", None) == "silent"


def test_effective_ok_when_both_fresh():
    """Healthy brain — heartbeat + receipt both inside threshold."""
    fresh_receipt = float(RECEIPT_STALE_AFTER_SECONDS - 60)
    assert _effective_tier("ok", fresh_receipt) == "ok"


def test_effective_ok_at_boundary():
    """Receipt exactly at threshold is still ok — strictly greater than
    flips to silent. Prevents flicker on the boundary."""
    assert _effective_tier("ok", float(RECEIPT_STALE_AFTER_SECONDS)) == "ok"


@pytest.mark.parametrize("hb_tier", ["stale", "dead", "unknown"])
def test_effective_passes_through_non_ok_heartbeat(hb_tier):
    """Heartbeat itself is unhealthy — dead heartbeat is a stronger
    operator signal than stale receipts. Don't silently mask it as
    `silent`; let the badge say what it actually is."""
    # Even with a fresh receipt, a non-ok heartbeat dominates.
    assert _effective_tier(hb_tier, 30.0) == hb_tier
    # And with no receipt at all, still pass through.
    assert _effective_tier(hb_tier, None) == hb_tier


def test_receipt_stale_threshold_is_reasonable():
    """Operator sanity check — the threshold must be longer than a
    single tick interval (45s × intent cooldown 6 = 270s) but short
    enough to catch hangs within minutes, not days."""
    assert RECEIPT_STALE_AFTER_SECONDS >= 300, "below tick × cooldown"
    assert RECEIPT_STALE_AFTER_SECONDS <= 3600, "too lax — would mask hangs"
