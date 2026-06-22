"""Tests for Hot-Brain Router + Regime Classifier + Mongo perf store
(2026-02-21).

Three layers:
  1. `hot_brain_router.py` — port of the operator's 23 router tests.
  2. `classifier.py` — regime decision tree.
  3. `brain_performance_store.py` — Mongo-backed perf lookup.

The router is DORMANT in production (no execution wiring). These
tests prove the kernel is correct before activation.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from shared.brains.brain_performance_store import (
    DOCTRINE_SIDECARS, get_recent_brain_performance,
)
from shared.brains.hot_brain_router import (
    BrainPerformance, RouteAction, RouterContext,
    classify_brain, compute_hot_score, route_hot_brain,
)
from shared.regime.classifier import (
    MarketSnapshot, Regime, classify_regime,
    is_range_bound, is_trending, is_volatile,
)
from db import db


# ──────────────────────── Router fixtures ──────────────────────────


def _hot_perf(**overrides) -> BrainPerformance:
    defaults = dict(
        brain="gto", lane="momentum", symbol="AAPL",
        trades=25, win_rate=0.68, avg_return_bps=72,
        profit_factor=1.72, max_drawdown_bps=-120,
        streak_wins=4, streak_losses=0,
        last_trade_at=datetime.now(timezone.utc) - timedelta(days=2),
        lane_win_rate=0.72, symbol_win_rate=0.65,
    )
    defaults.update(overrides)
    return BrainPerformance(**defaults)


def _cold_perf(**overrides) -> BrainPerformance:
    defaults = dict(
        brain="barracuda", lane="mean_reversion", symbol="TSLA",
        trades=25, win_rate=0.35, avg_return_bps=-44,
        profit_factor=0.65, max_drawdown_bps=-420,
        streak_wins=0, streak_losses=3,
        last_trade_at=datetime.now(timezone.utc) - timedelta(days=5),
        lane_win_rate=0.42, symbol_win_rate=0.38,
    )
    defaults.update(overrides)
    return BrainPerformance(**defaults)


def _neutral_perf(**overrides) -> BrainPerformance:
    defaults = dict(
        brain="midbrain", lane="swing", symbol="MSFT",
        trades=25, win_rate=0.50, avg_return_bps=12,
        profit_factor=1.05, max_drawdown_bps=-200,
        streak_wins=1, streak_losses=1,
        last_trade_at=datetime.now(timezone.utc) - timedelta(days=3),
        lane_win_rate=0.50, symbol_win_rate=0.50,
    )
    defaults.update(overrides)
    return BrainPerformance(**defaults)


def _unknown_perf(**overrides) -> BrainPerformance:
    defaults = dict(
        brain="hellcat", lane="breakout", symbol="NVDA",
        trades=4, win_rate=0.75, avg_return_bps=90,
        profit_factor=2.1, max_drawdown_bps=-50,
        streak_wins=3, streak_losses=0,
        last_trade_at=datetime.now(timezone.utc) - timedelta(days=1),
        lane_win_rate=0.75, symbol_win_rate=0.70,
    )
    defaults.update(overrides)
    return BrainPerformance(**defaults)


def _ctx(**overrides) -> RouterContext:
    defaults = dict(
        governor_size_mult=1.0, governor_vote_required=False,
        verifier_seat_tier="standard", roadguard_status="OPEN",
        current_portfolio_heat=0.3,
    )
    defaults.update(overrides)
    return RouterContext(**defaults)


# ──────────────────────── Classification ───────────────────────────


def test_hot_brain_classified_hot():
    assert classify_brain(_hot_perf()) == "HOT"


def test_cold_brain_classified_cold():
    assert classify_brain(_cold_perf()) == "COLD"


def test_neutral_brain_classified_neutral():
    assert classify_brain(_neutral_perf()) == "NEUTRAL"


def test_unknown_brain_classified_unknown():
    assert classify_brain(_unknown_perf()) == "UNKNOWN"


def test_trades_below_threshold_is_unknown():
    assert classify_brain(_hot_perf(trades=9)) == "UNKNOWN"


# ──────────────────────── Routing: HOT ─────────────────────────────


def test_hot_brain_elevates_when_governor_no_vote():
    d = route_hot_brain(_hot_perf(), _ctx(governor_vote_required=False))
    assert d.state == "HOT"
    assert d.route_action == RouteAction.ELEVATE
    assert d.size_multiplier_delta == pytest.approx(0.25 * 0.85)  # heat 0.3
    assert d.overrides_governor is True
    assert "elevated" in d.reason


def test_hot_brain_passes_when_governor_requires_vote():
    d = route_hot_brain(_hot_perf(), _ctx(governor_vote_required=True))
    assert d.route_action == RouteAction.PASS_THROUGH
    assert d.size_multiplier_delta == 0.0
    assert d.overrides_governor is False
    assert "vote_required" in d.reason


def test_hot_brain_reduced_by_portfolio_heat():
    d = route_hot_brain(_hot_perf(), _ctx(current_portfolio_heat=1.0))
    assert d.route_action == RouteAction.ELEVATE
    assert d.size_multiplier_delta == pytest.approx(0.25 * 0.5)


# ──────────────────────── Routing: NEUTRAL ─────────────────────────


def test_neutral_brain_passes_through():
    d = route_hot_brain(_neutral_perf(), _ctx())
    assert d.state == "NEUTRAL"
    assert d.route_action == RouteAction.PASS_THROUGH
    assert d.size_multiplier_delta == 0.0
    assert d.overrides_governor is False


# ──────────────────────── Routing: COLD ────────────────────────────


def test_cold_brain_blocks_when_no_lane_symbol_edge():
    d = route_hot_brain(_cold_perf(), _ctx())
    assert d.state == "COLD"
    assert d.route_action == RouteAction.BLOCK
    assert "no_mitigating_factors" in d.reason


def test_cold_brain_reduces_when_lane_symbol_favorable():
    d = route_hot_brain(
        _cold_perf(lane_win_rate=0.58, symbol_win_rate=0.55), _ctx(),
    )
    assert d.route_action == RouteAction.REDUCE
    assert d.size_multiplier_delta == -0.25
    assert "favorable" in d.reason


# ──────────────────────── Routing: UNKNOWN ─────────────────────────


def test_unknown_brain_reduces_not_blocks():
    d = route_hot_brain(_unknown_perf(), _ctx())
    assert d.state == "UNKNOWN"
    assert d.route_action == RouteAction.REDUCE
    assert d.size_multiplier_delta == -0.50
    assert "reduced_probe" in d.reason


def test_unknown_brain_with_roadguard_blocked():
    d = route_hot_brain(_unknown_perf(), _ctx(roadguard_status="BLOCKED"))
    assert d.route_action == RouteAction.BLOCK
    assert "roadguard" in d.reason


# ──────────────────────── Hard stops ───────────────────────────────


def test_roadguard_blocked_overrides_hot():
    d = route_hot_brain(_hot_perf(), _ctx(roadguard_status="BLOCKED"))
    assert d.route_action == RouteAction.BLOCK
    assert d.overrides_governor is False


def test_locked_seat_blocks_regardless_of_hot_score():
    d = route_hot_brain(_hot_perf(), _ctx(verifier_seat_tier="locked"))
    assert d.route_action == RouteAction.BLOCK
    assert "locked_by_pnl" in d.reason


def test_locked_seat_blocks_unknown():
    d = route_hot_brain(_unknown_perf(), _ctx(verifier_seat_tier="locked"))
    assert d.route_action == RouteAction.BLOCK


# ──────────────────────── Score computation ────────────────────────


def test_time_decay_reduces_old_scores():
    old = _hot_perf(last_trade_at=datetime.now(timezone.utc) - timedelta(days=90))
    new = _hot_perf(last_trade_at=datetime.now(timezone.utc) - timedelta(days=2))
    assert compute_hot_score(old) < compute_hot_score(new)


def test_streak_asymmetry_losses_weight_heavier():
    wins = _hot_perf(streak_wins=3, streak_losses=0)
    losses = _hot_perf(streak_wins=0, streak_losses=3)
    assert compute_hot_score(losses) < compute_hot_score(wins)


def test_lane_edge_boosts_score():
    strong = _neutral_perf(lane_win_rate=0.65)
    weak = _neutral_perf(lane_win_rate=0.35)
    assert compute_hot_score(strong) > compute_hot_score(weak)


# ──────────────────────── Final-size formula ───────────────────────


def test_final_size_multiplier_formula():
    p = _hot_perf()
    ctx = _ctx(governor_vote_required=False, current_portfolio_heat=0.0)
    d = route_hot_brain(p, ctx)
    final = max(0.0, min(ctx.governor_size_mult + d.size_multiplier_delta, 2.0))
    assert final == 1.25


def test_final_size_clamped_at_zero():
    p = _cold_perf(lane_win_rate=0.58, symbol_win_rate=0.55)
    ctx = _ctx(governor_size_mult=0.10)
    d = route_hot_brain(p, ctx)
    final = max(0.0, min(ctx.governor_size_mult + d.size_multiplier_delta, 2.0))
    assert final == 0.0


def test_final_size_clamped_at_two():
    p = _hot_perf()
    ctx = _ctx(governor_size_mult=1.90, governor_vote_required=False,
               current_portfolio_heat=0.0)
    d = route_hot_brain(p, ctx)
    final = max(0.0, min(ctx.governor_size_mult + d.size_multiplier_delta, 2.0))
    assert final == 2.0


# ──────────────────────── Regime classifier ────────────────────────


def _snapshot(**overrides) -> MarketSnapshot:
    defaults = dict(
        symbol="AAPL", price=100.0, open=100.0, high=100.5, low=99.5,
        close=100.2, volume=1_000_000, avg_volume_20d=1_000_000,
        atr_14=1.0, atr_14_avg=1.0, adx_14=15.0,
        bb_width=2.0, bb_width_avg=2.0,
        prev_high=100.0, prev_low=99.0, prev_close=99.8,
        session="regular", gap_pct=0.0,
    )
    defaults.update(overrides)
    return MarketSnapshot(**defaults)


def test_regime_low_vol_when_atr_below_threshold():
    r = classify_regime(_snapshot(atr_14=0.5, atr_14_avg=1.0))
    assert r.primary == Regime.LOW_VOL


def test_regime_high_vol_when_atr_above_threshold():
    r = classify_regime(_snapshot(atr_14=2.0, atr_14_avg=1.0))
    assert r.primary == Regime.HIGH_VOL


def test_regime_trend_up_when_adx_high_and_higher_high():
    r = classify_regime(_snapshot(
        adx_14=40.0, open=99.0, close=101.0, high=101.5, prev_high=100.5,
    ))
    assert r.primary == Regime.TREND_UP


def test_regime_chop_when_adx_low():
    # ADX=5 → chop strength 1.0 - (5/20) = 0.75 > normal default 0.5.
    r = classify_regime(_snapshot(adx_14=5.0))
    assert r.primary == Regime.CHOP


def test_regime_squeeze_when_bb_compressed():
    r = classify_regime(_snapshot(bb_width=1.0, bb_width_avg=2.0))
    # Squeeze ratio 0.5 < 0.6 threshold.
    assert "squeeze" in (r.primary, *r.secondary)


def test_regime_breakout_when_volume_and_range_spike():
    r = classify_regime(_snapshot(
        volume=3_000_000, avg_volume_20d=1_000_000,
        high=102.0, low=99.0, price=100.0,
    ))
    assert "breakout" in (r.primary, *r.secondary)


def test_regime_news_driven_off_hours_gap():
    r = classify_regime(_snapshot(gap_pct=2.5, session="pre"))
    assert "news_driven" in (r.primary, *r.secondary)


def test_regime_default_normal_when_nothing_stands_out():
    r = classify_regime(_snapshot())
    assert r.primary == Regime.NORMAL


def test_regime_helpers():
    assert is_trending(classify_regime(_snapshot(
        adx_14=40.0, open=99.0, close=101.0, high=101.5, prev_high=100.5,
    )))
    assert is_volatile(classify_regime(_snapshot(atr_14=2.0, atr_14_avg=1.0)))
    # ADX=5 makes CHOP win primary (0.75 > NORMAL's 0.5).
    assert is_range_bound(classify_regime(_snapshot(adx_14=5.0)))


# ──────────────────────── Mongo perf store ─────────────────────────


@pytest.fixture
def brain_id():
    return f"hbtest_{uuid.uuid4().hex[:8]}"


@pytest.fixture
async def cleanup_brain(brain_id):
    yield
    await db[DOCTRINE_SIDECARS].delete_many({"stack": brain_id})


async def _seed(brain: str, lane: str, symbol: str, pnl_usd: float,
                notional: float = 1000.0, days_ago: int = 0):
    ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    await db[DOCTRINE_SIDECARS].insert_one({
        "intent_id": f"hb_{uuid.uuid4().hex[:8]}",
        "stack": brain, "lane": lane, "symbol": symbol,
        "doctrine_version": "test_hot_brain_v1",
        "ts": ts,
        "outcome_join": {
            "joined_at": ts,
            "pnl_usd": pnl_usd,
            "notional_usd": notional,
            "lane": lane, "symbol": symbol,
        },
    })


@pytest.mark.asyncio
async def test_perf_store_returns_unknown_when_no_trades(brain_id, cleanup_brain):
    p = await get_recent_brain_performance(brain_id, "equity", "AAPL")
    assert p.trades == 0
    assert p.win_rate == 0.0
    # Router must classify this as UNKNOWN → REDUCE (not BLOCK).
    assert classify_brain(p) == "UNKNOWN"


@pytest.mark.asyncio
async def test_perf_store_aggregates_wins_and_losses(brain_id, cleanup_brain):
    # 7 wins, 3 losses for (brain, equity, AAPL).
    for _ in range(7):
        await _seed(brain_id, "equity", "AAPL", pnl_usd=50.0, days_ago=2)
    for _ in range(3):
        await _seed(brain_id, "equity", "AAPL", pnl_usd=-25.0, days_ago=2)
    p = await get_recent_brain_performance(brain_id, "equity", "AAPL")
    assert p.trades == 10
    assert p.win_rate == 0.7
    # avg_return_bps = ((7*500) + (3*-250)) / 10 = (3500-750)/10 = 275 bps
    assert p.avg_return_bps == pytest.approx(275.0, abs=1.0)
    # profit_factor = 3500 / 750 = 4.666...
    assert p.profit_factor == pytest.approx(4.666, abs=0.01)


@pytest.mark.asyncio
async def test_perf_store_respects_lane_filter(brain_id, cleanup_brain):
    # 5 wins in equity:AAPL, 5 losses in crypto:BTC. Equity query
    # should only see the wins.
    for _ in range(5):
        await _seed(brain_id, "equity", "AAPL", pnl_usd=50.0)
    for _ in range(5):
        await _seed(brain_id, "crypto", "BTC", pnl_usd=-50.0)
    p_eq = await get_recent_brain_performance(brain_id, "equity", "AAPL")
    assert p_eq.trades == 5
    assert p_eq.win_rate == 1.0


@pytest.mark.asyncio
async def test_perf_store_lookback_cap(brain_id, cleanup_brain):
    # Seed 30 trades; lookback=10 should only aggregate 10.
    for _ in range(30):
        await _seed(brain_id, "equity", "AAPL", pnl_usd=10.0)
    p = await get_recent_brain_performance(brain_id, "equity", "AAPL", lookback=10)
    assert p.trades == 10
