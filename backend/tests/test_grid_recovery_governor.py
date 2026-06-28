import pytest

from shared.strategies.grid_recovery_governor import (
    BasketSnapshot,
    BasketState,
    EscalationSeverity,
    GovernorModifier,
    evaluate_grid_recovery,
    evaluate_recovery_escalation,
    evaluate_take_profit,
    grid_step_price,
    lot_multiplier_for_add,
)


def base_basket(**overrides) -> BasketSnapshot:
    data = dict(
        symbol="NVDA",
        side="BUY",
        seat_id="seat_01",
        sleeve_id="sleeve_alpha",

        current_price=100.0,
        last_entry_price=101.0,
        weighted_avg_entry=101.0,
        atr_h1=1.0,
        spread_bps=10,
        spread_baseline_bps=10,

        state=BasketState.ADDING_ALLOWED,
        adds_count=0,
        max_adds=6,

        total_lots=1.0,
        floating_pnl_usd=-50.0,
        net_pnl_usd=-50.0,

        equity_at_birth=10000.0,
        equity_current=10000.0,
        account_dd_pct=0.0,
        margin_level=1000.0,
        sleeve_budget_remaining=1000.0,
        margin_required_for_next_add=100.0,

        mae_atr=0.0,
        age_days=0.0,

        zone_armed=True,
        regime_allows_side=True,
        trading_hour=True,
        kill_engine_halt=False,

        verifier_tier="BETA",
    )

    data.update(overrides)
    return BasketSnapshot(**data)


# ---------------------------------------------------
# Boundary tests
# ---------------------------------------------------

def test_returns_governor_modifier():
    modifier = evaluate_grid_recovery(base_basket())

    assert isinstance(modifier, GovernorModifier)
    assert not hasattr(modifier, "action")
    assert not hasattr(modifier, "next_state")


def test_modifier_is_immutable():
    modifier = evaluate_grid_recovery(base_basket())

    with pytest.raises(AttributeError):
        modifier.reason = "TEST"


# ---------------------------------------------------
# Grid spacing
# ---------------------------------------------------

def test_grid_step_floor():
    assert grid_step_price(100, 1) == 0.35
    assert grid_step_price(1000, 1) == 2.5


def test_negative_price_rejected():
    with pytest.raises(ValueError):
        grid_step_price(-1, 1)


def test_negative_atr_rejected():
    with pytest.raises(ValueError):
        grid_step_price(100, -1)


# ---------------------------------------------------
# Lot progression
# ---------------------------------------------------

def test_progression():
    assert lot_multiplier_for_add(0) == 1.0
    assert lot_multiplier_for_add(1) == 1.0
    assert lot_multiplier_for_add(2) == 1.0
    assert lot_multiplier_for_add(3) == 1.25
    assert lot_multiplier_for_add(4) == 1.25
    assert lot_multiplier_for_add(5) == 1.5
    assert lot_multiplier_for_add(6) == 0.0


# ---------------------------------------------------
# Grid eligibility
# ---------------------------------------------------

def test_grid_add_allowed():
    modifier = evaluate_grid_recovery(base_basket())

    assert modifier.reason == "GRID_ADD_ALLOWED"
    assert modifier.size_multiplier == 1.0


def test_grid_step_not_reached():
    modifier = evaluate_grid_recovery(
        base_basket(current_price=100.9)
    )

    assert modifier.reason == "GRID_STEP_NOT_REACHED"
    assert modifier.size_multiplier == 0.0


def test_zone_required():
    modifier = evaluate_grid_recovery(
        base_basket(zone_armed=False)
    )

    assert modifier.reason == "ZONE_NOT_ARMED"


def test_regime_required():
    modifier = evaluate_grid_recovery(
        base_basket(regime_allows_side=False)
    )

    assert modifier.reason == "REGIME_BLOCKS_SIDE"


def test_trading_hours_required():
    modifier = evaluate_grid_recovery(
        base_basket(trading_hour=False)
    )

    assert modifier.reason == "OUTSIDE_TRADING_HOURS"


def test_budget_guard():
    modifier = evaluate_grid_recovery(
        base_basket(
            sleeve_budget_remaining=50,
            margin_required_for_next_add=100,
        )
    )

    assert modifier.reason == "INSUFFICIENT_SLEEVE_BUDGET"


def test_spread_guard():
    modifier = evaluate_grid_recovery(
        base_basket(
            spread_bps=40,
            spread_baseline_bps=10,
        )
    )

    assert modifier.reason == "SPREAD_TOO_WIDE_FOR_GRID_ADD"


def test_max_adds():
    modifier = evaluate_grid_recovery(
        base_basket(adds_count=6)
    )

    assert modifier.reason == "MAX_ADDS_REACHED"


# ---------------------------------------------------
# Recovery ladder
# ---------------------------------------------------

def test_warning():
    modifier = evaluate_grid_recovery(
        base_basket(mae_atr=6)
    )

    assert modifier.severity == EscalationSeverity.WARNING
    assert modifier.vote_required


def test_critical():
    modifier = evaluate_grid_recovery(
        base_basket(mae_atr=9)
    )

    assert modifier.severity == EscalationSeverity.CRITICAL


def test_fatal():
    modifier = evaluate_grid_recovery(
        base_basket(mae_atr=11)
    )

    assert modifier.severity == EscalationSeverity.FATAL


def test_account_drawdown_fatal():
    modifier = evaluate_grid_recovery(
        base_basket(account_dd_pct=15)
    )

    assert modifier.severity == EscalationSeverity.FATAL


# ---------------------------------------------------
# Take Profit
# ---------------------------------------------------

def test_equity_target():
    modifier = evaluate_grid_recovery(
        base_basket(
            floating_pnl_usd=50,
            current_price=102,
        )
    )

    assert modifier.reason.startswith("ADVISORY")
    assert modifier.vote_required


def test_atr_target_buy():
    modifier = evaluate_grid_recovery(
        base_basket(
            current_price=101.6,
            weighted_avg_entry=101,
            atr_h1=1,
        )
    )

    assert modifier.reason == "ADVISORY_ATR_DISTANCE_TP"


def test_recovery_breakeven():
    modifier = evaluate_take_profit(
        base_basket(
            state=BasketState.RECOVERY,
            net_pnl_usd=5,
        )
    )

    assert modifier is not None


# ---------------------------------------------------
# Verifier tiers
# ---------------------------------------------------

def test_alpha_more_rope():
    # mae_atr=5.5 sits between BETA's recovery threshold (5.0) and
    # ALPHA's tier-adjusted recovery threshold (5.0 × 1.2 = 6.0).
    # Under BETA the same value fires WARNING; under ALPHA it
    # returns None — that's the doctrine of "ALPHA gets more rope".
    modifier = evaluate_recovery_escalation(
        base_basket(
            verifier_tier="ALPHA",
            mae_atr=5.5,
        )
    )

    assert modifier is None


def test_alpha_more_rope_proven_by_beta_warning():
    # The companion: same input under BETA does fire — proving the
    # gap is the verifier tier, not the absolute value.
    modifier = evaluate_recovery_escalation(
        base_basket(
            verifier_tier="BETA",
            mae_atr=5.5,
        )
    )

    assert modifier is not None
    assert modifier.severity == EscalationSeverity.WARNING


def test_alpha_mae_11_is_critical_not_fatal():
    modifier = evaluate_recovery_escalation(
        base_basket(
            verifier_tier="ALPHA",
            mae_atr=11,
        )
    )

    assert modifier is not None
    assert modifier.severity == EscalationSeverity.CRITICAL
    assert modifier.reason == "CRITICAL_CRISIS_THRESHOLD"


def test_beta_fatal():
    modifier = evaluate_recovery_escalation(
        base_basket(
            verifier_tier="BETA",
            mae_atr=11,
        )
    )

    assert modifier.severity == EscalationSeverity.FATAL


def test_gamma_fatal():
    modifier = evaluate_recovery_escalation(
        base_basket(
            verifier_tier="GAMMA",
            mae_atr=11,
        )
    )

    assert modifier.severity == EscalationSeverity.FATAL


def test_margin_thresholds():
    alpha = evaluate_recovery_escalation(
        base_basket(
            verifier_tier="ALPHA",
            margin_level=280,
        )
    )

    beta = evaluate_recovery_escalation(
        base_basket(
            verifier_tier="BETA",
            margin_level=280,
        )
    )

    gamma = evaluate_recovery_escalation(
        base_basket(
            verifier_tier="GAMMA",
            margin_level=280,
        )
    )

    assert alpha is None
    assert beta.severity == EscalationSeverity.CRITICAL
    assert gamma.severity == EscalationSeverity.CRITICAL


# ---------------------------------------------------
# Integration
# ---------------------------------------------------

def test_modifier_composition():
    governor_size = 2.0

    modifier = evaluate_grid_recovery(
        base_basket(adds_count=3)
    )

    assert governor_size * modifier.size_multiplier == 2.5


def test_zero_multiplier():
    modifier = evaluate_grid_recovery(
        base_basket(mae_atr=9)
    )

    assert modifier.size_multiplier == 0.0
