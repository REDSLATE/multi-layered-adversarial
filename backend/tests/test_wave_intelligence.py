from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from shared.wave_intelligence import (
    AUTHORITY,
    WaveIntelligenceMachine,
    WaveMode,
    summarize_wave_observations,
)


def _bars(
    closes: list[float],
    *,
    widths: list[float] | None = None,
    minute_offset: int = 0,
) -> list[dict]:
    start = datetime(2026, 8, 1, tzinfo=timezone.utc)
    rows = []
    for idx, close in enumerate(closes):
        previous = closes[idx - 1] if idx else close
        width = widths[idx] if widths else 0.30
        rows.append(
            {
                "o": previous,
                "h": max(previous, close) + width,
                "l": min(previous, close) - width,
                "c": close,
                "v": 1000 + idx,
                "ts": (start + timedelta(minutes=idx + minute_offset)).isoformat(),
            }
        )
    return rows


def _trend_bars(*, minute_offset: int = 0) -> list[dict]:
    closes = [100.0 + idx * 0.80 for idx in range(30)]
    return _bars(closes, widths=[0.15] * len(closes), minute_offset=minute_offset)


def _range_bars(*, minute_offset: int = 0) -> list[dict]:
    closes = [100.0 + math.sin(idx * 1.10) * 1.20 for idx in range(30)]
    return _bars(closes, widths=[0.20] * len(closes), minute_offset=minute_offset)


def test_clean_trend_enters_trend_follow_mode():
    observation = WaveIntelligenceMachine().evaluate(
        symbol="nvda",
        lane="equity",
        timeframe="1m",
        bars=_trend_bars(),
    )

    assert observation.mode == WaveMode.TREND_FOLLOW
    assert observation.bias.value == "BULLISH"
    assert observation.scores.trend >= 0.62
    assert observation.scores.trend > observation.scores.range


def test_oscillation_enters_range_grid_observation_mode():
    observation = WaveIntelligenceMachine().evaluate(
        symbol="ETH/USD",
        lane="crypto",
        timeframe="1m",
        bars=_range_bars(),
    )

    assert observation.mode == WaveMode.RANGE_GRID
    assert observation.bias.value == "NEUTRAL"
    assert observation.scores.range >= 0.60
    assert observation.scores.range > observation.scores.trend


def test_large_closed_bar_preempts_with_danger_pause():
    closes = [100.0 + math.sin(idx) * 0.10 for idx in range(29)] + [112.0]
    widths = [0.10] * 29 + [3.0]
    observation = WaveIntelligenceMachine().evaluate(
        symbol="BTC/USD",
        lane="crypto",
        timeframe="1m",
        bars=_bars(closes, widths=widths),
    )

    assert observation.mode == WaveMode.DANGER_PAUSE
    assert observation.scores.danger >= 0.72
    assert "WAVE_PRICE_SHOCK" in observation.reason_codes


def test_insufficient_history_is_an_honest_wait():
    observation = WaveIntelligenceMachine().evaluate(
        symbol="AAPL",
        lane="equity",
        timeframe="5m",
        bars=_bars([100.0 + idx * 0.1 for idx in range(8)]),
    )

    assert observation.mode == WaveMode.WAIT
    assert observation.data_quality == "INSUFFICIENT"
    assert observation.scores.trend == 0.0
    assert "WAVE_INSUFFICIENT_BARS" in observation.reason_codes


def test_same_source_bar_is_idempotent():
    machine = WaveIntelligenceMachine()
    first = machine.evaluate(
        symbol="NVDA",
        lane="equity",
        timeframe="1m",
        bars=_trend_bars(),
    )
    duplicate = machine.evaluate(
        symbol="NVDA",
        lane="equity",
        timeframe="1m",
        bars=_trend_bars(),
    )

    assert duplicate is first
    assert duplicate.observation_id == first.observation_id
    assert duplicate.cooldown_remaining == first.cooldown_remaining


def test_cooldown_prevents_one_bar_mode_flapping():
    machine = WaveIntelligenceMachine()
    trend = machine.evaluate(
        symbol="NVDA",
        lane="equity",
        timeframe="1m",
        bars=_trend_bars(),
    )
    range_one = machine.evaluate(
        symbol="NVDA",
        lane="equity",
        timeframe="1m",
        bars=_range_bars(minute_offset=30),
    )
    range_two = machine.evaluate(
        symbol="NVDA",
        lane="equity",
        timeframe="1m",
        bars=_range_bars(minute_offset=60),
    )
    range_three = machine.evaluate(
        symbol="NVDA",
        lane="equity",
        timeframe="1m",
        bars=_range_bars(minute_offset=90),
    )

    assert trend.mode == WaveMode.TREND_FOLLOW
    assert range_one.mode == WaveMode.TREND_FOLLOW
    assert range_two.mode == WaveMode.TREND_FOLLOW
    assert "WAVE_COOLDOWN_HOLD" in range_one.reason_codes
    assert range_three.mode == WaveMode.RANGE_GRID


def test_observation_has_no_execution_authority_fields():
    payload = (
        WaveIntelligenceMachine()
        .evaluate(
            symbol="NVDA",
            lane="equity",
            timeframe="1m",
            bars=_trend_bars(),
        )
        .to_dict()
    )

    assert payload["authority"] == AUTHORITY
    assert payload["can_execute"] is False
    assert payload["can_size"] is False
    assert payload["can_block"] is False
    assert "action" not in payload
    assert "size_multiplier" not in payload
    assert "intent" not in payload


def test_pulse_summary_stays_compact_and_deduplicated_from_brains():
    machine = WaveIntelligenceMachine()
    trend = machine.evaluate(
        symbol="NVDA",
        lane="equity",
        timeframe="1m",
        bars=_trend_bars(),
    ).to_dict()
    danger_closes = [100.0 + math.sin(idx) * 0.10 for idx in range(29)] + [112.0]
    danger = (
        WaveIntelligenceMachine()
        .evaluate(
            symbol="BTC/USD",
            lane="crypto",
            timeframe="1m",
            bars=_bars(danger_closes, widths=[0.10] * 29 + [3.0]),
        )
        .to_dict()
    )

    summary = summarize_wave_observations([trend, danger])

    assert summary["authority"] == AUTHORITY
    assert summary["observations"] == 2
    assert summary["mode_counts"]["TREND_FOLLOW"] == 1
    assert summary["mode_counts"]["DANGER_PAUSE"] == 1
    assert summary["danger_symbols"] == ["BTC/USD"]
    assert "scores" not in summary
    assert "reason_codes" not in summary
