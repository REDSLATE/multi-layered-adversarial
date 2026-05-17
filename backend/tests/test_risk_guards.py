"""Tests for deterministic risk guards (pure math).

Lane-neutral by construction — these guards have no DB / async / LLM
dependencies, so we can exercise the full decision matrix in plain
function calls.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from shared.risk.max_hold_time_guard import max_hold_time_guard
from shared.risk.stop_loss_guard import stop_loss_guard
from shared.risk.trailing_stop_guard import trailing_stop_guard


# ─── StopLoss ─────────────────────────────────────────────────────────

def test_stop_loss_long_triggers_when_loss_exceeds_threshold():
    v = stop_loss_guard(side="LONG", entry_price=100.0, current_price=97.5, stop_loss_pct=2.0)
    assert v.action == "CLOSE"
    assert v.close_fraction == 1.0
    assert v.pnl_pct < 0


def test_stop_loss_long_holds_when_within_threshold():
    v = stop_loss_guard(side="LONG", entry_price=100.0, current_price=99.0, stop_loss_pct=2.0)
    assert v.action == "HOLD"
    assert v.close_fraction == 0.0


def test_stop_loss_short_triggers_when_price_rises_against():
    v = stop_loss_guard(side="SHORT", entry_price=100.0, current_price=103.0, stop_loss_pct=2.0)
    assert v.action == "CLOSE"


def test_stop_loss_short_holds_when_price_drops_in_favor():
    v = stop_loss_guard(side="SHORT", entry_price=100.0, current_price=98.0, stop_loss_pct=2.0)
    assert v.action == "HOLD"
    assert v.pnl_pct > 0


def test_stop_loss_threshold_magnitude_normalized():
    # passing a negative threshold should behave identically — we use abs()
    v_pos = stop_loss_guard(side="LONG", entry_price=100.0, current_price=97.0, stop_loss_pct=2.0)
    v_neg = stop_loss_guard(side="LONG", entry_price=100.0, current_price=97.0, stop_loss_pct=-2.0)
    assert v_pos.action == v_neg.action == "CLOSE"


# ─── TrailingStop ─────────────────────────────────────────────────────

def test_trailing_stop_inactive_until_activation_threshold():
    # Up only 0.5% — below activate_after=1%, so no trail yet.
    v = trailing_stop_guard(
        side="LONG", entry_price=100.0, current_price=100.5,
        previous_peak=100.5, trail_pct=1.5, activate_after_pct=1.0,
    )
    assert v.action == "HOLD"
    assert "inactive" in v.reason.lower()


def test_trailing_stop_long_close_after_peak_drawdown():
    # Peak 105, current 103 → 1.90% drawdown ≥ trail 1.5% → CLOSE
    v = trailing_stop_guard(
        side="LONG", entry_price=100.0, current_price=103.0,
        previous_peak=105.0, trail_pct=1.5, activate_after_pct=1.0,
    )
    assert v.action == "CLOSE"
    assert v.new_peak == 105.0


def test_trailing_stop_long_holds_inside_trail_band():
    # Peak 105, current 104.5 → 0.48% drawdown < trail 1.5% → HOLD
    v = trailing_stop_guard(
        side="LONG", entry_price=100.0, current_price=104.5,
        previous_peak=105.0, trail_pct=1.5, activate_after_pct=1.0,
    )
    assert v.action == "HOLD"


def test_trailing_stop_short_close_when_price_rises_from_trough():
    # Entry 100, trough 95, current 97 → 2.1% rise from trough ≥ 1.5% → CLOSE
    v = trailing_stop_guard(
        side="SHORT", entry_price=100.0, current_price=97.0,
        previous_peak=95.0, trail_pct=1.5, activate_after_pct=1.0,
    )
    assert v.action == "CLOSE"
    assert v.new_peak == 95.0  # trough preserved


def test_trailing_stop_updates_peak_on_new_high():
    v = trailing_stop_guard(
        side="LONG", entry_price=100.0, current_price=106.0,
        previous_peak=104.0, trail_pct=1.5, activate_after_pct=1.0,
    )
    assert v.new_peak == 106.0
    assert v.action == "HOLD"  # fresh high, no drawdown


# ─── MaxHoldTime ──────────────────────────────────────────────────────

def test_max_hold_time_close_when_held_too_long():
    opened = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    v = max_hold_time_guard(opened_at=opened, max_hold_minutes=60.0)
    assert v.action == "CLOSE"
    assert v.held_for_minutes > 60.0


def test_max_hold_time_holds_when_fresh():
    opened = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    v = max_hold_time_guard(opened_at=opened, max_hold_minutes=60.0)
    assert v.action == "HOLD"


def test_max_hold_time_handles_invalid_opened_at():
    v = max_hold_time_guard(opened_at="not-an-iso-string", max_hold_minutes=60.0)
    assert v.action == "HOLD"
    assert "invalid" in v.reason.lower()


def test_max_hold_time_supports_pinned_now_for_tests():
    opened_dt = datetime(2026, 2, 17, 12, 0, 0, tzinfo=timezone.utc)
    pinned_now = opened_dt + timedelta(minutes=120)
    v = max_hold_time_guard(
        opened_at=opened_dt.isoformat(),
        max_hold_minutes=60.0,
        now=pinned_now,
    )
    assert v.action == "CLOSE"
    assert abs(v.held_for_minutes - 120.0) < 0.1


def test_max_hold_time_z_suffix_iso_parses():
    opened = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat().replace("+00:00", "Z")
    v = max_hold_time_guard(opened_at=opened, max_hold_minutes=60.0)
    assert v.action == "CLOSE"
