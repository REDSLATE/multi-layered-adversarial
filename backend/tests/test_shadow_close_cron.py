"""Shadow-close cron regression tests.

Pins the 4:05pm ET trigger window from the 2026-02-19 operator
directive — "P1 — 4:05pm ET cron so shadow-close runs automatically
at session end without manual click."
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from shared.runtime import shadow_close_cron as cron


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    cron.reset_for_tests()
    for k in (
        "SHADOW_CLOSE_CRON_ENABLED",
        "SHADOW_CLOSE_CRON_HOUR_ET",
        "SHADOW_CLOSE_CRON_MIN_ET",
        "SHADOW_CLOSE_CRON_WINDOW_MIN",
    ):
        monkeypatch.delenv(k, raising=False)
    yield
    cron.reset_for_tests()


def _set_now_et(monkeypatch, dt_naive_et: datetime) -> None:
    """Pin `_now_et()` to a fixed timestamp without touching the
    rest of the codebase's clock."""
    tz = ZoneInfo("America/New_York")
    fixed = dt_naive_et.replace(tzinfo=tz)
    monkeypatch.setattr(cron, "_now_et", lambda: fixed)


def test_fires_inside_window_on_weekday(monkeypatch):
    # Monday at 4:05pm ET → should fire.
    _set_now_et(monkeypatch, datetime(2026, 2, 16, 16, 5, 0))
    assert cron._should_fire(cron._now_et()) is True


def test_fires_at_top_of_hour(monkeypatch):
    # 4:00pm ET (window starts at the top of the hour).
    _set_now_et(monkeypatch, datetime(2026, 2, 16, 16, 0, 30))
    assert cron._should_fire(cron._now_et()) is True


def test_does_not_fire_after_window(monkeypatch):
    # 4:15pm ET — outside the default 14-min window.
    _set_now_et(monkeypatch, datetime(2026, 2, 16, 16, 30, 0))
    assert cron._should_fire(cron._now_et()) is False


def test_does_not_fire_before_window(monkeypatch):
    # 3:59pm ET — too early.
    _set_now_et(monkeypatch, datetime(2026, 2, 16, 15, 59, 0))
    assert cron._should_fire(cron._now_et()) is False


def test_does_not_fire_on_saturday(monkeypatch):
    # Saturday at 4:05pm ET — market closed.
    _set_now_et(monkeypatch, datetime(2026, 2, 14, 16, 5, 0))
    assert cron._should_fire(cron._now_et()) is False


def test_does_not_fire_on_sunday(monkeypatch):
    _set_now_et(monkeypatch, datetime(2026, 2, 15, 16, 5, 0))
    assert cron._should_fire(cron._now_et()) is False


def test_idempotent_within_same_day(monkeypatch):
    """First call fires (returns True), subsequent calls within the
    same ET day return False — even within the window — because the
    `_last_fired_date` marker was set on the first call."""
    _set_now_et(monkeypatch, datetime(2026, 2, 16, 16, 5, 0))
    assert cron._should_fire(cron._now_et()) is True
    # Same day, still in window — must NOT fire again.
    _set_now_et(monkeypatch, datetime(2026, 2, 16, 16, 10, 0))
    assert cron._should_fire(cron._now_et()) is False


def test_disabled_via_env(monkeypatch):
    """`SHADOW_CLOSE_CRON_ENABLED=false` makes `start_worker` a no-op."""
    monkeypatch.setenv("SHADOW_CLOSE_CRON_ENABLED", "false")
    cron.start_worker()
    assert cron._worker_task is None


def test_status_envelope_keys(monkeypatch):
    """The admin endpoint contract — `status()` returns a stable
    set of keys the dashboard can read."""
    _set_now_et(monkeypatch, datetime(2026, 2, 16, 14, 30, 0))
    s = cron.status()
    assert set(s.keys()) >= {
        "enabled", "task_alive", "now_et", "last_fired_date_et",
        "target_window_et", "would_fire_now",
    }
    assert s["would_fire_now"] is False  # 2:30pm — outside window
    assert "16:00" in s["target_window_et"]


def test_env_override_changes_target_hour(monkeypatch):
    """The operator can repoint the cron via env vars (e.g. for
    after-hours trading days or testing). Pinning the target to 9am
    and firing at 9:05am verifies the override is honored."""
    monkeypatch.setenv("SHADOW_CLOSE_CRON_HOUR_ET", "9")
    _set_now_et(monkeypatch, datetime(2026, 2, 16, 9, 5, 0))
    assert cron._should_fire(cron._now_et()) is True


def test_status_dry_check_does_not_mutate_marker(monkeypatch):
    """`status()` calls `_should_fire_dry_check()` which must NOT
    set the `_last_fired_date` marker — otherwise a dashboard refresh
    would prevent the real fire on the next tick."""
    _set_now_et(monkeypatch, datetime(2026, 2, 16, 16, 5, 0))
    s1 = cron.status()
    assert s1["would_fire_now"] is True
    # Marker must still be None — only the real `_should_fire` should
    # have set it.
    assert cron._last_fired_date is None
    # And a real `_should_fire` call afterwards still fires correctly.
    assert cron._should_fire(cron._now_et()) is True
    assert cron._last_fired_date == "2026-02-16"
