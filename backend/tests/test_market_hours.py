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


def test_matches_tier_1_blocks_equity_after_hours(monkeypatch):
    """The whole point of this module — `matches_tier_1` MUST refuse
    equity intents when `is_equity_rth()` returns False."""
    from shared import auto_submit_policy
    from shared.auto_submit_policy import matches_tier_1, set_policy

    monkeypatch.delenv("RISEDUAL_BYPASS_MARKET_HOURS", raising=False)
    set_policy(
        enabled=True,
        confidence_min=0.0,
        allowed_actions=["BUY", "SELL"],
        allowed_lanes=["equity", "crypto"],
        allowed_brains=["camaro"],
        required_dry_run_state="passed",
    )

    # Freeze "now" to a Sunday by monkeypatching market_hours.
    import shared.market_hours as mh
    monkeypatch.setattr(mh, "is_equity_rth", lambda now_utc=None: False)
    monkeypatch.setattr(
        mh, "market_hours_reason",
        lambda now_utc=None: "equity_after_hours: weekend",
    )

    intent = {
        "action": "BUY",
        "lane": "equity",
        "stack": "camaro",
        "confidence": 0.99,
        "dry_run_state": "passed",
    }
    ok, reason = matches_tier_1(intent)
    assert ok is False
    assert reason.startswith("equity_after_hours")

    # Crypto on the SAME closed clock must still pass.
    intent_crypto = {
        "action": "BUY",
        "lane": "crypto",
        "stack": "camaro",
        "confidence": 0.99,
        "dry_run_state": "passed",
    }
    ok2, reason2 = matches_tier_1(intent_crypto)
    assert ok2 is True, f"crypto must trade 24/7: {reason2}"

    auto_submit_policy.reset_policy_for_tests()
