"""Tests for the US equity market-hours gate.

Doctrine pin (operator, 2026-02-20): Webull 417s any equity order
outside RTH. The auto-submitter consults `is_equity_rth()` so we
don't waste API budget and post-mortem rows on DOA orders. These
tests pin DST handling, weekend rejection, holiday rejection, and
the bypass override.
"""
import sys
from datetime import datetime, timezone

sys.path.insert(0, "/app/backend")

import pytest

from shared.market_hours import (
    is_equity_rth,
    market_hours_reason,
    next_rth_open_iso,
)


# The async equity-after-hours test below uses pytest-asyncio.
# Scope to that test only so the bulk of the synchronous market-
# hours pinning tests don't pay the event-loop overhead.


@pytest.fixture(autouse=True)
def _no_bypass(monkeypatch):
    monkeypatch.delenv("RISEDUAL_BYPASS_MARKET_HOURS", raising=False)
    yield


# ── Open-window cases ──────────────────────────────────────────────


def test_rth_mid_session_summer_dst():
    """Tue 2026-06-16 14:00 UTC = 10:00 ET (EDT). Inside RTH."""
    t = datetime(2026, 6, 16, 14, 0, tzinfo=timezone.utc)
    assert is_equity_rth(t) is True


def test_rth_mid_session_winter_est():
    """Tue 2026-12-15 15:00 UTC = 10:00 ET (EST). Inside RTH."""
    t = datetime(2026, 12, 15, 15, 0, tzinfo=timezone.utc)
    assert is_equity_rth(t) is True


def test_rth_open_inclusive():
    """09:30 ET sharp is the first traded minute — must be RTH."""
    # 09:30 EDT = 13:30 UTC in summer
    t = datetime(2026, 6, 16, 13, 30, tzinfo=timezone.utc)
    assert is_equity_rth(t) is True


def test_rth_close_exclusive():
    """16:00 ET sharp is the close — orders at exactly 16:00 reject."""
    # 16:00 EDT = 20:00 UTC in summer
    t = datetime(2026, 6, 16, 20, 0, tzinfo=timezone.utc)
    assert is_equity_rth(t) is False


# ── Closed-window cases ────────────────────────────────────────────


def test_rejects_pre_market():
    """08:00 ET Tue — pre-market, equity orders 417."""
    t = datetime(2026, 6, 16, 12, 0, tzinfo=timezone.utc)
    assert is_equity_rth(t) is False


def test_rejects_after_hours():
    """20:00 ET Tue — after-hours."""
    t = datetime(2026, 6, 17, 0, 0, tzinfo=timezone.utc)  # midnight UTC = 20:00 ET Tue
    assert is_equity_rth(t) is False


def test_rejects_saturday():
    """Sat 2026-06-13 mid-day UTC — weekend, closed."""
    t = datetime(2026, 6, 13, 14, 0, tzinfo=timezone.utc)
    assert is_equity_rth(t) is False


def test_rejects_sunday():
    t = datetime(2026, 6, 14, 14, 0, tzinfo=timezone.utc)
    assert is_equity_rth(t) is False


# ── Holiday rejection ──────────────────────────────────────────────


def test_rejects_christmas_2026():
    """Christmas falls on Fri 2026-12-25. Closed even at 10:00 ET."""
    t = datetime(2026, 12, 25, 15, 0, tzinfo=timezone.utc)
    assert is_equity_rth(t) is False


def test_rejects_thanksgiving_2026():
    """Thu 2026-11-26 — market closed."""
    t = datetime(2026, 11, 26, 15, 0, tzinfo=timezone.utc)
    assert is_equity_rth(t) is False


def test_rejects_july4_observed_2026():
    """July 4 2026 is Sat → market closes Fri July 3 (observed)."""
    t = datetime(2026, 7, 3, 15, 0, tzinfo=timezone.utc)
    assert is_equity_rth(t) is False


# ── Operator bypass ────────────────────────────────────────────────


def test_bypass_forces_open(monkeypatch):
    """`RISEDUAL_BYPASS_MARKET_HOURS=true` overrides — useful for
    backtests against a live SDK or one-off after-hours pokes."""
    monkeypatch.setenv("RISEDUAL_BYPASS_MARKET_HOURS", "true")
    # Sunday — would normally be closed
    t = datetime(2026, 6, 14, 14, 0, tzinfo=timezone.utc)
    assert is_equity_rth(t) is True


@pytest.mark.parametrize("val", ["false", "0", "no", "", "  "])
def test_bypass_off_values_dont_override(monkeypatch, val):
    monkeypatch.setenv("RISEDUAL_BYPASS_MARKET_HOURS", val)
    t = datetime(2026, 6, 14, 14, 0, tzinfo=timezone.utc)  # Sunday
    assert is_equity_rth(t) is False


# ── next_rth_open_iso ──────────────────────────────────────────────


def test_next_open_from_weekend():
    """Sat 2026-06-13 → next open is Mon 2026-06-15 13:30 UTC."""
    t = datetime(2026, 6, 13, 14, 0, tzinfo=timezone.utc)
    out = next_rth_open_iso(t)
    assert out.startswith("2026-06-15T13:30")


def test_next_open_skips_holiday():
    """Christmas 2026 is Fri. Next open after Wed Dec 23 should
    be Thu Dec 24 (regular weekday), and after Christmas
    morning → Mon Dec 28."""
    # Standing at Christmas morning 10:00 ET
    t = datetime(2026, 12, 25, 15, 0, tzinfo=timezone.utc)
    out = next_rth_open_iso(t)
    # Next open is Mon Dec 28 09:30 EST = 14:30 UTC
    assert out.startswith("2026-12-28T14:30")


def test_next_open_after_close():
    """16:30 ET Tue → next open is Wed 09:30 ET."""
    t = datetime(2026, 6, 16, 20, 30, tzinfo=timezone.utc)
    out = next_rth_open_iso(t)
    assert out.startswith("2026-06-17T13:30")


# ── Audit reason strings ───────────────────────────────────────────


def test_reason_holiday():
    t = datetime(2026, 12, 25, 15, 0, tzinfo=timezone.utc)
    r = market_hours_reason(t)
    assert r.startswith("equity_after_hours")
    assert "holiday" in r
    assert "next open" in r


def test_reason_weekend():
    t = datetime(2026, 6, 13, 14, 0, tzinfo=timezone.utc)
    r = market_hours_reason(t)
    assert "weekend" in r
    assert "Saturday" in r


def test_reason_outside_rth():
    """Monday at 08:00 ET — pre-market."""
    t = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
    r = market_hours_reason(t)
    assert "outside RTH" in r
    assert "08:00" in r


# ── Integration: auto-submit gate ──────────────────────────────────
# (Historical integration test `test_matches_tier_1_blocks_equity_after_hours`
# removed 2026-07-04 — imported `shared.auto_submit_policy` which was
# deleted in the 2026-07-01 refactor. Market-hours logic itself is
# still covered by the unit tests above; the integration path against
# the current execution flow lives in
# `test_seat_council_participant_doctrine.py` and related suites.)
