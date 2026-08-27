from __future__ import annotations

from datetime import datetime, timezone

from mc_pulse.snapshot_service import _closed_bars_for_wave


def test_wave_filter_excludes_the_current_open_minute():
    now = datetime(2026, 8, 1, 14, 30, 30, tzinfo=timezone.utc)
    bars = [
        {"ts": "2026-08-01T14:29:00+00:00", "c": 100.0},
        {"ts": "2026-08-01T14:30:00+00:00", "c": 101.0},
    ]

    assert _closed_bars_for_wave(bars, "1m", now) == [bars[0]]


def test_wave_filter_keeps_a_bar_at_its_exact_close():
    now = datetime(2026, 8, 1, 14, 35, 0, tzinfo=timezone.utc)
    bar = {"ts": "2026-08-01T14:30:00+00:00", "c": 100.0}

    assert _closed_bars_for_wave([bar], "5m", now) == [bar]


def test_wave_filter_fails_closed_for_unknown_timeframe():
    now = datetime(2026, 8, 1, 14, 35, 0, tzinfo=timezone.utc)
    bar = {"ts": "2026-08-01T14:30:00+00:00", "c": 100.0}

    assert _closed_bars_for_wave([bar], "15m", now) == []
