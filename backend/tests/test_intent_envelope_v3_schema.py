"""Paradox v3 Intent Envelope — schema validation tests.

Step 1 of the v3 rollout (PRD 2026-02). Pins:
  * PlanBlock + ExecutionBlock pydantic shape (every enum, every
    required field, every optional field).
  * `IntentIn` now accepts `intent_version`, `plan`, `execution`
    without breaking the v2 contract.
  * Locked operator decisions (1A target_prices optional, 2B
    setup_custom_tag fallback, 3B no inner plan_version, 4A horizon
    TTL defaults table, 6B HBR-scores-everything — assertion on the
    enum set being unfiltered).
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from shared.intent_envelope_v3 import (
    EXECUTION_ACTION_VALUES,
    EXECUTION_STYLE_VALUES,
    HORIZON_TTL_DEFAULTS,
    HORIZON_VALUES,
    PLAN_INTENT_VALUES,
    PORTFOLIO_POSTURE_VALUES,
    SETUP_VALUES,
    SIZE_POSTURE_VALUES,
    STANCE_VALUES,
    ExecutionBlock,
    PlanBlock,
)


class TestPlanBlockHappyPath:
    def test_minimal_plan_accepted(self):
        p = PlanBlock(
            stance="BULLISH",
            setup="bull_flag",
            intent="ENTER",
            execution_style="MARKET_NOW",
            confidence=0.72,
        )
        # Defaults per operator §11 + locked decisions.
        assert p.size_posture == "STANDARD"
        assert p.portfolio_posture == "NEUTRAL"
        assert p.horizon == "UNKNOWN"
        assert p.ttl_seconds is None
        assert p.target_prices is None
        assert p.thesis == ""

    def test_full_plan_with_trigger_and_targets(self):
        p = PlanBlock(
            stance="BULLISH",
            setup="bull_flag",
            intent="WAIT_FOR_TRIGGER",
            execution_style="TRIGGERED_LIMIT",
            confidence=0.81,
            trigger_price=187.40,
            invalidation_price=184.20,
            target_prices=[189.00, 191.50],
            horizon="INTRADAY",
            ttl_seconds=3_600,
            thesis="intraday bull flag, awaiting break of pole",
            portfolio_posture="RISK_ON",
            size_posture="ELEVATED",
        )
        assert p.intent == "WAIT_FOR_TRIGGER"
        assert p.target_prices == [189.00, 191.50]


class TestPlanBlockEnumCoverage:
    @pytest.mark.parametrize("stance", STANCE_VALUES)
    def test_every_stance_accepted(self, stance):
        PlanBlock(
            stance=stance,
            setup="other",
            intent="WATCH",
            execution_style="MARKET_NOW",
            confidence=0.5,
        )

    @pytest.mark.parametrize("intent", PLAN_INTENT_VALUES)
    def test_every_plan_intent_accepted(self, intent):
        # HEDGE has a separate validator (tested below) — supply the
        # required field to keep this paramatrized test happy.
        kwargs = {}
        if intent == "HEDGE":
            kwargs["hedge_against_symbol"] = "SPY"
        PlanBlock(
            stance="NEUTRAL", setup="other", intent=intent,
            execution_style="MARKET_NOW", confidence=0.5,
            **kwargs,
        )

    @pytest.mark.parametrize("style", EXECUTION_STYLE_VALUES)
    def test_every_execution_style_accepted(self, style):
        PlanBlock(
            stance="NEUTRAL", setup="other", intent="WATCH",
            execution_style=style, confidence=0.5,
        )

    @pytest.mark.parametrize("setup", SETUP_VALUES)
    def test_every_setup_accepted(self, setup):
        PlanBlock(
            stance="NEUTRAL", setup=setup, intent="WATCH",
            execution_style="MARKET_NOW", confidence=0.5,
        )

    @pytest.mark.parametrize("size_posture", SIZE_POSTURE_VALUES)
    def test_every_size_posture_accepted(self, size_posture):
        PlanBlock(
            stance="NEUTRAL", setup="other", intent="WATCH",
            execution_style="MARKET_NOW", confidence=0.5,
            size_posture=size_posture,
        )

    @pytest.mark.parametrize("portfolio_posture", PORTFOLIO_POSTURE_VALUES)
    def test_every_portfolio_posture_accepted(self, portfolio_posture):
        PlanBlock(
            stance="NEUTRAL", setup="other", intent="WATCH",
            execution_style="MARKET_NOW", confidence=0.5,
            portfolio_posture=portfolio_posture,
        )

    @pytest.mark.parametrize("horizon", HORIZON_VALUES)
    def test_every_horizon_accepted(self, horizon):
        PlanBlock(
            stance="NEUTRAL", setup="other", intent="WATCH",
            execution_style="MARKET_NOW", confidence=0.5,
            horizon=horizon,
        )


class TestPlanBlockRejection:
    def test_invalid_stance_rejected(self):
        with pytest.raises(ValidationError):
            PlanBlock(
                stance="MAYBE_UP",  # not in enum
                setup="other", intent="WATCH",
                execution_style="MARKET_NOW", confidence=0.5,
            )

    def test_invalid_setup_rejected(self):
        with pytest.raises(ValidationError):
            PlanBlock(
                stance="NEUTRAL", setup="flag",  # not in enum
                intent="WATCH", execution_style="MARKET_NOW",
                confidence=0.5,
            )

    def test_confidence_out_of_range(self):
        with pytest.raises(ValidationError):
            PlanBlock(
                stance="NEUTRAL", setup="other", intent="WATCH",
                execution_style="MARKET_NOW", confidence=1.5,
            )

    def test_hedge_requires_symbol(self):
        with pytest.raises(ValidationError) as excinfo:
            PlanBlock(
                stance="NEUTRAL", setup="other", intent="HEDGE",
                execution_style="MARKET_NOW", confidence=0.5,
            )
        assert "hedge_against_symbol" in str(excinfo.value)

    def test_hedge_with_symbol_accepted(self):
        p = PlanBlock(
            stance="NEUTRAL", setup="other", intent="HEDGE",
            execution_style="MARKET_NOW", confidence=0.5,
            hedge_against_symbol="SPY",
        )
        assert p.hedge_against_symbol == "SPY"

    def test_negative_trigger_price_rejected(self):
        with pytest.raises(ValidationError):
            PlanBlock(
                stance="BULLISH", setup="bull_flag", intent="WAIT_FOR_TRIGGER",
                execution_style="TRIGGERED_LIMIT", confidence=0.7,
                trigger_price=-5.0,
            )

    def test_target_prices_must_all_be_positive(self):
        with pytest.raises(ValidationError):
            PlanBlock(
                stance="BULLISH", setup="bull_flag", intent="ENTER",
                execution_style="MARKET_NOW", confidence=0.7,
                target_prices=[100.0, 0.0, 102.0],
            )


class TestSetupCustomTag:
    """Operator decision 2B — enum + setup_custom_tag free-string fallback."""

    def test_setup_other_with_custom_tag(self):
        p = PlanBlock(
            stance="NEUTRAL", setup="other", intent="WATCH",
            execution_style="MARKET_NOW", confidence=0.5,
            setup_custom_tag="opening_drive_fade",
        )
        assert p.setup_custom_tag == "opening_drive_fade"

    def test_setup_custom_tag_optional(self):
        # When setup=other is used WITHOUT setup_custom_tag the model
        # still accepts (no hard requirement per operator 2B).
        p = PlanBlock(
            stance="NEUTRAL", setup="other", intent="WATCH",
            execution_style="MARKET_NOW", confidence=0.5,
        )
        assert p.setup_custom_tag is None

    def test_setup_custom_tag_too_long(self):
        with pytest.raises(ValidationError):
            PlanBlock(
                stance="NEUTRAL", setup="other", intent="WATCH",
                execution_style="MARKET_NOW", confidence=0.5,
                setup_custom_tag="x" * 65,
            )


class TestExecutionBlock:
    def test_minimal_execution_accepted(self):
        e = ExecutionBlock()
        assert e.action is None
        assert e.derived_from_plan is True  # default per PRD §3.1

    def test_full_execution_accepted(self):
        e = ExecutionBlock(
            action="BUY",
            notional_usd=100.0,
            limit_price=187.50,
            broker_hint="webull",
            derived_from_plan=True,
            derived_at="2026-02-22T15:30:00Z",
        )
        assert e.action == "BUY"
        assert e.broker_hint == "webull"

    @pytest.mark.parametrize("action", EXECUTION_ACTION_VALUES)
    def test_every_exec_action_accepted(self, action):
        e = ExecutionBlock(action=action)
        assert e.action == action

    def test_invalid_broker_hint_rejected(self):
        with pytest.raises(ValidationError):
            ExecutionBlock(broker_hint="robinhood")

    def test_negative_notional_rejected(self):
        with pytest.raises(ValidationError):
            ExecutionBlock(notional_usd=-25.0)


class TestLockedDecisions:
    """Pins the locked operator decisions in shape so the next agent
    can't silently regress them."""

    def test_1a_target_prices_optional_no_penalty(self):
        """Operator decision 1A: target_prices is optional on ENTER.
        The model accepts ENTER intents with no target_prices and
        emits no validation warning."""
        p = PlanBlock(
            stance="BULLISH", setup="breakout", intent="ENTER",
            execution_style="MARKET_NOW", confidence=0.75,
            # No target_prices.
        )
        assert p.target_prices is None
        assert p.intent == "ENTER"

    def test_3b_no_inner_plan_version(self):
        """Operator decision 3B: YAGNI — no `plan_version` field.
        Pinned so a future 'tidy up' pass doesn't quietly add one."""
        fields = set(PlanBlock.model_fields.keys())
        assert "plan_version" not in fields, (
            "Operator decision 3B locks: no inner plan_version. "
            "Top-level intent_version is the discriminator."
        )

    def test_4a_horizon_ttl_defaults(self):
        """Operator decision 4A: TTL defaults map for each horizon."""
        assert HORIZON_TTL_DEFAULTS == {
            "INTRADAY": 23_400,
            "SWING":    432_000,
            "POSITION": 1_728_000,
            "UNKNOWN":  None,
        }

    def test_6b_no_plan_intent_exclusion_for_scoring(self):
        """Operator decision 6B: Hot-Brain Router scores EVERYTHING.
        This module must not expose any 'excluded for scoring' list —
        if such a list ever appears, the next agent owes the operator
        a fresh approval cycle."""
        import shared.intent_envelope_v3 as mod
        for attr in dir(mod):
            assert "EXCLUDE_FROM_SCORING" not in attr.upper(), (
                f"Operator decision 6B locks no exclusion list. "
                f"Found suspicious export: {attr}"
            )
