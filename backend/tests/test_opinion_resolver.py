"""Tripwires — opinion market resolver (2026-05-24).

Pins the doctrine:

  1. Only DIRECTIONAL stances (long/short) are eligible for
     auto-resolution. observation / endorse / veto must NEVER be
     graded by this worker.
  2. Win/loss thresholds use lane-aware bars (crypto ±2%, equity ±1%).
  3. `long` price ↑ = win, `short` price ↓ = win (sided PnL).
  4. Anchor-less opinions are skipped, never poisoned.
  5. The worker is idempotent: re-running cannot create duplicate
     outcomes for the same opinion_id.
  6. Outcomes carry `resolved_by="auto:market-data"`.
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, patch

from shared.opinion_resolver import (
    DIRECTIONAL_STANCES,
    OUTCOME_THRESHOLDS,
    _classify_outcome,
    _grade_opinion,
    _lane_for_topic,
    _sided_pnl_pct,
    _symbol_from_topic,
)


pytestmark = [pytest.mark.tripwire]


# ─── pure helpers ──────────────────────────────────────────────────


def test_directional_stances_locked():
    """Only long and short are auto-resolvable. If a future change
    tries to add `observation` etc. to this set, the doctrine of
    'only directional stances graded from price' breaks."""
    assert DIRECTIONAL_STANCES == {"long", "short"}


def test_outcome_thresholds_lane_aware():
    """Crypto's volatility means ±2%; equity's tighter at ±1%. Don't
    let these drift without conscious operator decision."""
    assert OUTCOME_THRESHOLDS["crypto"] == 0.02
    assert OUTCOME_THRESHOLDS["equity"] == 0.01


def test_classify_outcome_thresholds():
    assert _classify_outcome(0.025, "crypto") == "win"     # >2%
    assert _classify_outcome(-0.025, "crypto") == "loss"
    assert _classify_outcome(0.005, "crypto") == "no-event"  # <2%
    assert _classify_outcome(0.011, "equity") == "win"     # >1%
    assert _classify_outcome(-0.011, "equity") == "loss"
    assert _classify_outcome(0.003, "equity") == "no-event"


def test_sided_pnl_long_up_is_positive():
    """Long position: price rose from 100 → 110 = +10%."""
    assert _sided_pnl_pct("long", 100.0, 110.0) == pytest.approx(0.10)


def test_sided_pnl_long_down_is_negative():
    """Long position: price fell from 100 → 90 = -10%."""
    assert _sided_pnl_pct("long", 100.0, 90.0) == pytest.approx(-0.10)


def test_sided_pnl_short_down_is_positive():
    """Short position: price fell from 100 → 90 = +10% (short wins)."""
    assert _sided_pnl_pct("short", 100.0, 90.0) == pytest.approx(0.10)


def test_sided_pnl_short_up_is_negative():
    """Short position: price rose from 100 → 110 = -10% (short loses)."""
    assert _sided_pnl_pct("short", 100.0, 110.0) == pytest.approx(-0.10)


def test_sided_pnl_safe_on_zero_anchor():
    """Anchor 0 must not divide-by-zero."""
    assert _sided_pnl_pct("long", 0.0, 100.0) == 0.0


def test_lane_for_topic_crypto():
    """Common quote suffixes route to crypto."""
    assert _lane_for_topic("symbol:BTCUSD") == "crypto"
    assert _lane_for_topic("symbol:ETHUSDT") == "crypto"
    assert _lane_for_topic("symbol:BTC") == "crypto"
    assert _lane_for_topic("symbol:SOL") == "crypto"


def test_lane_for_topic_equity():
    assert _lane_for_topic("symbol:AAPL") == "equity"
    assert _lane_for_topic("symbol:SPY") == "equity"
    assert _lane_for_topic("symbol:GOOGL") == "equity"


def test_symbol_from_topic():
    assert _symbol_from_topic("symbol:btc") == "BTC"
    assert _symbol_from_topic("symbol:aapl") == "AAPL"
    assert _symbol_from_topic("") is None
    assert _symbol_from_topic("no_colon_here") is None


# ─── grading behavior ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_observation_stance_never_resolved():
    """The doctrinal hard guard: non-directional stances are returned
    untouched by `_grade_opinion`."""
    op = {
        "opinion_id": "tw-obs-1",
        "runtime": "camaro",
        "stance": "observation",
        "topic": "symbol:AAPL",
        "posted_at": "2026-05-22T10:00:00+00:00",
        "anchor_price": 100.0,
    }
    result = await _grade_opinion(op)
    assert result is None


@pytest.mark.asyncio
async def test_endorse_stance_never_resolved():
    op = {
        "opinion_id": "tw-end-1",
        "runtime": "camaro",
        "stance": "endorse",
        "topic": "symbol:AAPL",
        "posted_at": "2026-05-22T10:00:00+00:00",
        "anchor_price": 100.0,
    }
    assert await _grade_opinion(op) is None


@pytest.mark.asyncio
async def test_veto_stance_never_resolved():
    op = {
        "opinion_id": "tw-veto-1",
        "runtime": "chevelle",
        "stance": "veto",
        "topic": "symbol:AAPL",
        "posted_at": "2026-05-22T10:00:00+00:00",
        "anchor_price": 100.0,
    }
    assert await _grade_opinion(op) is None


@pytest.mark.asyncio
async def test_too_young_skipped():
    """Opinions inside the resolution horizon must NOT be graded."""
    from datetime import datetime, timezone
    op = {
        "opinion_id": "tw-young-1",
        "runtime": "alpha",
        "stance": "long",
        "topic": "symbol:AAPL",
        "posted_at": datetime.now(timezone.utc).isoformat(),  # right now
        "anchor_price": 100.0,
    }
    with patch(
        "shared.opinion_resolver._fetch_current_price",
        new=AsyncMock(return_value=110.0),
    ):
        assert await _grade_opinion(op) is None


@pytest.mark.asyncio
async def test_no_anchor_skipped():
    """Without anchor_price, the opinion cannot be graded — return None
    so it stays unresolved (operator may backfill or future writer
    may add anchor at post time)."""
    op = {
        "opinion_id": "tw-no-anchor-1",
        "runtime": "alpha",
        "stance": "long",
        "topic": "symbol:AAPL",
        "posted_at": "2026-05-20T10:00:00+00:00",  # well past horizon
        # no anchor_price
    }
    assert await _grade_opinion(op) is None


@pytest.mark.asyncio
async def test_long_win_graded():
    """Long opinion + price rose enough → win."""
    op = {
        "opinion_id": "tw-long-win",
        "runtime": "alpha",
        "stance": "long",
        "topic": "symbol:AAPL",
        "posted_at": "2026-05-20T10:00:00+00:00",
        "anchor_price": 100.0,
        "confidence": 0.7,
    }
    with patch(
        "shared.opinion_resolver._fetch_current_price",
        new=AsyncMock(return_value=105.0),  # +5%, equity threshold 1%
    ):
        result = await _grade_opinion(op)
    assert result is not None
    assert result["actual"] == "win"
    assert result["resolved_by"] == "auto:market-data"
    assert result["pnl_pct"] == pytest.approx(0.05)
    assert result["opinion_id"] == "tw-long-win"
    assert result["runtime"] == "alpha"
    assert result["stance"] == "long"


@pytest.mark.asyncio
async def test_long_loss_graded():
    op = {
        "opinion_id": "tw-long-loss",
        "runtime": "alpha",
        "stance": "long",
        "topic": "symbol:AAPL",
        "posted_at": "2026-05-20T10:00:00+00:00",
        "anchor_price": 100.0,
    }
    with patch(
        "shared.opinion_resolver._fetch_current_price",
        new=AsyncMock(return_value=95.0),  # -5%
    ):
        result = await _grade_opinion(op)
    assert result["actual"] == "loss"


@pytest.mark.asyncio
async def test_short_win_graded():
    """Short opinion + price fell = win."""
    op = {
        "opinion_id": "tw-short-win",
        "runtime": "redeye",
        "stance": "short",
        "topic": "symbol:TSLA",
        "posted_at": "2026-05-20T10:00:00+00:00",
        "anchor_price": 200.0,
    }
    with patch(
        "shared.opinion_resolver._fetch_current_price",
        new=AsyncMock(return_value=190.0),  # -5%, short wins
    ):
        result = await _grade_opinion(op)
    assert result["actual"] == "win"
    assert result["stance"] == "short"


@pytest.mark.asyncio
async def test_short_loss_graded():
    """Short opinion + price rose = loss."""
    op = {
        "opinion_id": "tw-short-loss",
        "runtime": "redeye",
        "stance": "short",
        "topic": "symbol:TSLA",
        "posted_at": "2026-05-20T10:00:00+00:00",
        "anchor_price": 200.0,
    }
    with patch(
        "shared.opinion_resolver._fetch_current_price",
        new=AsyncMock(return_value=210.0),
    ):
        result = await _grade_opinion(op)
    assert result["actual"] == "loss"


@pytest.mark.asyncio
async def test_no_event_graded():
    """Tiny move below threshold = no-event."""
    op = {
        "opinion_id": "tw-noevent",
        "runtime": "alpha",
        "stance": "long",
        "topic": "symbol:AAPL",
        "posted_at": "2026-05-20T10:00:00+00:00",
        "anchor_price": 100.0,
    }
    with patch(
        "shared.opinion_resolver._fetch_current_price",
        new=AsyncMock(return_value=100.2),  # +0.2% under 1% threshold
    ):
        result = await _grade_opinion(op)
    assert result["actual"] == "no-event"


@pytest.mark.asyncio
async def test_crypto_threshold_is_wider():
    """Crypto symbol with +1.5% move = no-event (below 2% bar);
    equity symbol with +1.5% = win (above 1% bar)."""
    base = {
        "runtime": "camaro",
        "stance": "long",
        "posted_at": "2026-05-20T10:00:00+00:00",
        "anchor_price": 100.0,
    }
    with patch(
        "shared.opinion_resolver._fetch_current_price",
        new=AsyncMock(return_value=101.5),  # +1.5%
    ):
        crypto = await _grade_opinion({**base, "opinion_id": "tw-c1", "topic": "symbol:BTC"})
        equity = await _grade_opinion({**base, "opinion_id": "tw-e1", "topic": "symbol:AAPL"})
    assert crypto["actual"] == "no-event"
    assert equity["actual"] == "win"


@pytest.mark.asyncio
async def test_no_price_returns_none_for_retry():
    """If the price fetcher returns None (broker disconnected, etc.),
    grading returns None so the next tick retries."""
    op = {
        "opinion_id": "tw-noprice",
        "runtime": "alpha",
        "stance": "long",
        "topic": "symbol:AAPL",
        "posted_at": "2026-05-20T10:00:00+00:00",
        "anchor_price": 100.0,
    }
    with patch(
        "shared.opinion_resolver._fetch_current_price",
        new=AsyncMock(return_value=None),
    ):
        assert await _grade_opinion(op) is None
