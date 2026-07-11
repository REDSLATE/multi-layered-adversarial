"""Freshness gate contract tests.

2026-07 iter-27 doctrine: a snapshot with `health.status != "fresh"`
MUST NOT reach a brain. Pins the exact behavior against the
stale-feeder incident of 2026-07-11 (472 identical NVDA intents
from a 20-hour-old bar).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from mc_pulse.freshness import (
    MAX_BAR_AGE_SECONDS,
    SnapshotHealth,
    evaluate_snapshot_health,
)


# ─────────────── crypto (24/7) ───────────────

def test_crypto_fresh_bar_is_fresh():
    now = datetime(2026, 7, 11, 20, 0, 0, tzinfo=timezone.utc)
    latest = now - timedelta(minutes=2)  # 5m tf → 900s cap; 120s fine
    h = evaluate_snapshot_health(lane="crypto", tf="5m",
                                  latest_bar_at=latest, now=now)
    assert h.status == "fresh"
    assert h.is_fresh


def test_crypto_20h_old_bar_is_stale():
    """The exact class of failure from 2026-07-11 — a 20-hour-old
    bar during active trading. Doctrine: STALE, never fresh."""
    now = datetime(2026, 7, 11, 20, 0, 0, tzinfo=timezone.utc)
    latest = now - timedelta(hours=20)
    h = evaluate_snapshot_health(lane="crypto", tf="5m",
                                  latest_bar_at=latest, now=now)
    assert h.status == "stale"
    assert not h.is_fresh
    assert "STALE_MARKET_DATA" in h.reason_codes


def test_crypto_missing_bar_reports_missing():
    now = datetime(2026, 7, 11, 20, 0, 0, tzinfo=timezone.utc)
    h = evaluate_snapshot_health(lane="crypto", tf="5m",
                                  latest_bar_at=None, now=now)
    assert h.status == "missing"
    assert "NO_BAR_AVAILABLE" in h.reason_codes


# ─────────────── equity (session-aware) ───────────────

def test_equity_rth_fresh_bar_is_fresh():
    """Friday 3pm ET (=19:00 UTC during DST): market open,
    2-min-old 5m bar should be fresh."""
    now = datetime(2026, 7, 10, 19, 0, 0, tzinfo=timezone.utc)  # Fri RTH
    latest = now - timedelta(minutes=2)
    h = evaluate_snapshot_health(lane="equity", tf="5m",
                                  latest_bar_at=latest, now=now)
    assert h.is_fresh


def test_equity_rth_20h_old_bar_is_stale():
    """The exact incident. Friday RTH, latest bar from yesterday's
    close (20h stale). Must be stale."""
    now = datetime(2026, 7, 10, 19, 0, 0, tzinfo=timezone.utc)  # Fri RTH
    latest = now - timedelta(hours=20)
    h = evaluate_snapshot_health(lane="equity", tf="5m",
                                  latest_bar_at=latest, now=now)
    assert h.status == "stale"
    assert "STALE_MARKET_DATA" in h.reason_codes


def test_equity_saturday_3am_old_bar_is_fresh():
    """Doctrine acknowledgement: at 3am Saturday, Friday's final
    NVDA bar is NOT broken merely because it's old. Session-
    aware freshness must recognize this."""
    now = datetime(2026, 7, 11, 3, 0, 0, tzinfo=timezone.utc)   # Sat
    latest = datetime(2026, 7, 10, 20, 0, 0, tzinfo=timezone.utc)  # Fri near-close
    h = evaluate_snapshot_health(lane="equity", tf="5m",
                                  latest_bar_at=latest, now=now)
    assert h.is_fresh
    assert "OUTSIDE_RTH_LAST_SESSION_BAR" in h.reason_codes


def test_equity_weekend_ancient_bar_still_stale():
    """Outside RTH gives grace but not infinite grace. A bar
    5 days old is stale even on Sunday."""
    now = datetime(2026, 7, 12, 3, 0, 0, tzinfo=timezone.utc)   # Sun
    latest = datetime(2026, 7, 6, 20, 0, 0, tzinfo=timezone.utc)  # 6 days ago
    h = evaluate_snapshot_health(lane="equity", tf="5m",
                                  latest_bar_at=latest, now=now)
    assert h.status == "stale"


# ─────────────── invariants ───────────────

def test_future_bar_flagged_invalid():
    now = datetime(2026, 7, 11, 20, 0, 0, tzinfo=timezone.utc)
    latest = now + timedelta(minutes=5)
    h = evaluate_snapshot_health(lane="crypto", tf="5m",
                                  latest_bar_at=latest, now=now)
    assert h.status == "invalid"
    assert "BAR_FROM_FUTURE" in h.reason_codes


def test_max_age_covers_expected_tfs():
    for tf in ("1m", "5m", "15m", "1h", "1d"):
        assert tf in MAX_BAR_AGE_SECONDS
        assert MAX_BAR_AGE_SECONDS[tf] > 0


def test_health_frozen():
    """SnapshotHealth is an audit record — must be immutable
    once constructed."""
    h = SnapshotHealth(status="fresh", latest_bar_at=None,
                       age_seconds=0.0, max_age_seconds=900)
    with pytest.raises((AttributeError, TypeError)):
        h.status = "stale"        # type: ignore[misc]
