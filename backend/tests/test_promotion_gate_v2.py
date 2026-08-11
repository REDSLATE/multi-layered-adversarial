import sys
sys.path.insert(0, "/app/backend")

from datetime import datetime, timedelta, timezone

from shared.forensics.promotion_gate_v2 import (
    PromotionConfig,
    PromotionGate,
    PromotionState,
    EvaluationObservation,
    ExecutionFill,
)


T0 = datetime(2026, 8, 1, tzinfo=timezone.utc)


def obs(i, gross, epoch="maker-v1", **kw):
    return EvaluationObservation(
        observation_id=str(i),
        resolved_at=T0 + timedelta(minutes=30 * i),
        gross_return_pct=gross,
        epoch_id=epoch,
        **kw,
    )


def fill(i, fee=0.04, slip=0.04, epoch="maker-v1", maker=True):
    ref = 100.0
    price = ref * (1 + slip / 100.0)
    return ExecutionFill(
        fill_id=f"f{i}",
        trade_id=f"t{i}",
        timestamp=T0 + timedelta(minutes=i),
        side="BUY",
        fill_price=price,
        reference_price=ref,
        quantity=1.0,
        fee_pct=fee,
        liquidity="maker" if maker else "taker",
        epoch_id=epoch,
    )


def test_assumed_cost_used_until_measured_sample():
    cfg = PromotionConfig(min_observations=2, min_elapsed_hours=0, min_measured_fills=5)
    d = PromotionGate(cfg).evaluate([obs(1, .4), obs(2, .4)], [fill(1), fill(2)], "maker-v1")
    assert d.cost.source == "assumed"
    assert d.cost.round_trip_cost_pct == cfg.assumed_round_trip_cost_pct


def test_measured_cost_replaces_assumption():
    cfg = PromotionConfig(
        min_observations=2,
        min_elapsed_hours=0,
        min_measured_fills=4,
        assumed_round_trip_cost_pct=.30,
    )
    fills = [fill(i, fee=.04, slip=.04) for i in range(4)]
    d = PromotionGate(cfg).evaluate([obs(1, .3), obs(2, .3)], fills, "maker-v1")
    assert d.cost.source == "measured"
    # .04 fee + .04 slippage = .08% leg; x2 = .16% round trip
    assert round(d.cost.round_trip_cost_pct, 6) == .16


def test_old_epoch_does_not_poison_current_epoch():
    cfg = PromotionConfig(
        recent_window_observations=10,
        min_observations=3,
        min_elapsed_hours=0,
        min_measured_fills=100,
        assumed_round_trip_cost_pct=.10,
        max_drawdown_per_100_obs_pct=100,
        min_profit_factor=.5,
    )
    old = [obs(i, -5.0, epoch="old") for i in range(50)]
    new = [obs(100+i, .5, epoch="maker-v1") for i in range(3)]
    d = PromotionGate(cfg).evaluate(old + new, [], "maker-v1")
    assert d.evaluated_observations == 3
    assert d.lifetime_observations == 53
    assert d.net_expectancy_pct > 0
    assert d.state == PromotionState.PASS


def test_hard_safety_failure_always_hard_stops():
    cfg = PromotionConfig(
        min_observations=1,
        min_elapsed_hours=0,
        min_profit_factor=0,
        max_drawdown_per_100_obs_pct=999,
    )
    d = PromotionGate(cfg).evaluate(
        [obs(1, 1.0, risk_violation=True)],
        [],
        "maker-v1",
    )
    assert d.state == PromotionState.HARD_STOP


def test_positive_edge_impossible_drawdown_becomes_recalibration_not_endless_fail():
    cfg = PromotionConfig(
        recent_window_observations=300,
        min_observations=100,
        min_elapsed_hours=0,
        assumed_round_trip_cost_pct=0.0,
        min_measured_fills=999,
        min_profit_factor=1.0,
        max_drawdown_per_100_obs_pct=1.0,
        recalibration_min_observations=200,
        recalibration_drawdown_multiple=2.0,
    )
    # Positive expectancy/PF overall, but large clustered loss creates a drawdown
    # structurally far above an intentionally tiny 1% target.
    returns = ([1.0] * 160) + ([-2.0] * 40) + ([1.0] * 50)
    observations = [obs(i, r) for i, r in enumerate(returns)]
    d = PromotionGate(cfg).evaluate(observations, [], "maker-v1")

    assert d.net_expectancy_pct > 0
    assert d.profit_factor >= 1.0
    assert "observation_drawdown_per_100" in d.recalibration_candidates
    assert d.state == PromotionState.NEEDS_RECALIBRATION


def test_insufficient_sample_is_fail_not_recalibration():
    cfg = PromotionConfig(min_observations=100, min_elapsed_hours=0)
    d = PromotionGate(cfg).evaluate([obs(i, 1.0) for i in range(10)], [], "maker-v1")
    assert d.state == PromotionState.FAIL
