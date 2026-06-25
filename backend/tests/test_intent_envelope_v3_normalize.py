"""Paradox v3 — read-side `normalize_intent` lifter tests.

Step 1 doctrine: v2 docs + v3 docs must produce the same normalised
shape so every downstream consumer (funnel, post-mortem, verifier,
frontend, etc.) can read `doc["execution"]["action"]` and
`doc["plan"]["intent"]` without branching on version.

PRD §6.2 mapping table is the source of truth.
"""
from __future__ import annotations

import pytest

from shared.intent_envelope_v3 import normalize_intent, normalize_intents


# ─── v2 → v3 lift per §6.2 mapping table ──────────────────────────
class TestV2Lift:
    def test_buy_lifts_to_enter_bullish(self):
        doc = {
            "intent_id": "abc",
            "action": "BUY",
            "confidence": 0.7,
            "rationale": "above_vwap + macd_bullish",
        }
        out = normalize_intent(doc)
        assert out["intent_version"] == "v2"
        assert out["plan"]["stance"] == "BULLISH"
        assert out["plan"]["intent"] == "ENTER"
        assert out["plan"]["execution_style"] == "MARKET_NOW"
        assert out["plan"]["confidence"] == 0.7
        assert out["execution"]["action"] == "BUY"
        assert out["execution"]["derived_from_plan"] is False  # v2 fast-path

    def test_sell_lifts_to_exit_bearish(self):
        doc = {"action": "SELL", "confidence": 0.55}
        out = normalize_intent(doc)
        assert out["plan"]["stance"] == "BEARISH"
        assert out["plan"]["intent"] == "EXIT"
        assert out["execution"]["action"] == "SELL"

    def test_short_lifts_to_enter_bearish(self):
        doc = {"action": "SHORT", "confidence": 0.6}
        out = normalize_intent(doc)
        assert out["plan"]["stance"] == "BEARISH"
        assert out["plan"]["intent"] == "ENTER"
        assert out["execution"]["action"] == "SHORT"

    def test_cover_lifts_to_exit_bullish(self):
        doc = {"action": "COVER", "confidence": 0.5}
        out = normalize_intent(doc)
        assert out["plan"]["stance"] == "BULLISH"
        assert out["plan"]["intent"] == "EXIT"
        assert out["execution"]["action"] == "COVER"

    def test_hold_lifts_to_watch_neutral_with_null_action(self):
        """The critical mapping — HOLD becomes execution.action=null
        and plan.intent=WATCH per operator §11 locked decision."""
        doc = {"action": "HOLD", "confidence": 0.3}
        out = normalize_intent(doc)
        assert out["plan"]["stance"] == "NEUTRAL"
        assert out["plan"]["intent"] == "WATCH"
        assert out["execution"]["action"] is None

    def test_open_lifts_to_enter(self):
        doc = {"action": "OPEN", "confidence": 0.5}
        out = normalize_intent(doc)
        assert out["plan"]["intent"] == "ENTER"
        assert out["execution"]["action"] == "OPEN"

    def test_close_lifts_to_exit(self):
        doc = {"action": "CLOSE", "confidence": 0.5}
        out = normalize_intent(doc)
        assert out["plan"]["intent"] == "EXIT"
        assert out["execution"]["action"] == "CLOSE"

    def test_v2_target_and_stop_prices_lift_into_plan(self):
        doc = {
            "action": "BUY", "confidence": 0.7,
            "target_price": 100.0, "stop_price": 95.0,
        }
        out = normalize_intent(doc)
        assert out["plan"]["target_prices"] == [100.0]
        assert out["plan"]["invalidation_price"] == 95.0

    def test_v2_doc_preserves_all_legacy_fields(self):
        """v2 fields must remain ON the lifted doc — old consumers
        keep reading `doc["action"]` after the lift."""
        doc = {
            "intent_id": "abc",
            "stack": "camino",
            "symbol": "AAPL",
            "lane": "equity",
            "action": "BUY",
            "confidence": 0.7,
            "rationale": "test",
            "executed": False,
            "gate_state": "pending",
        }
        out = normalize_intent(doc)
        for k, v in doc.items():
            assert out[k] == v, f"v2 field {k!r} was mutated"

    def test_input_not_mutated(self):
        doc = {"action": "BUY", "confidence": 0.5}
        normalize_intent(doc)
        assert "plan" not in doc
        assert "execution" not in doc


class TestV3PassThrough:
    def test_wait_for_trigger_passes_through(self):
        doc = {
            "intent_version": "v3",
            "intent_id": "abc",
            "action": None,
            "plan": {
                "stance": "BULLISH",
                "setup": "bull_flag",
                "intent": "WAIT_FOR_TRIGGER",
                "execution_style": "TRIGGERED_LIMIT",
                "confidence": 0.81,
                "trigger_price": 187.40,
                "invalidation_price": 184.20,
            },
        }
        out = normalize_intent(doc)
        assert out["intent_version"] == "v3"
        assert out["plan"]["intent"] == "WAIT_FOR_TRIGGER"
        assert out["plan"]["trigger_price"] == 187.40
        # Missing execution block gets defaulted (no action).
        assert out["execution"]["action"] is None
        assert out["execution"]["derived_from_plan"] is True

    def test_partial_v3_plan_gets_defaults_filled(self):
        """A v3 doc with only the required plan fields comes back with
        every optional inner key populated to its default — so the
        consumer never has to handle KeyError on optional keys."""
        doc = {
            "intent_version": "v3",
            "plan": {
                "stance": "BULLISH", "setup": "breakout",
                "intent": "ENTER", "execution_style": "MARKET_NOW",
                "confidence": 0.7,
            },
        }
        out = normalize_intent(doc)
        # All optional plan keys are present after lift.
        for k in ("size_posture", "portfolio_posture", "hedge_against_symbol",
                  "trigger_price", "invalidation_price", "target_prices",
                  "thesis", "horizon", "ttl_seconds", "setup_custom_tag"):
            assert k in out["plan"], f"missing default for {k!r}"
        assert out["plan"]["size_posture"] == "STANDARD"
        assert out["plan"]["portfolio_posture"] == "NEUTRAL"

    def test_v3_full_envelope_passes_through(self):
        doc = {
            "intent_version": "v3",
            "plan": {
                "stance": "BULLISH", "setup": "bull_flag",
                "intent": "ENTER", "execution_style": "MARKET_NOW",
                "size_posture": "ELEVATED", "portfolio_posture": "RISK_ON",
                "confidence": 0.82, "thesis": "high conviction breakout",
                "target_prices": [102.0, 105.0], "invalidation_price": 95.0,
                "horizon": "INTRADAY", "ttl_seconds": 7_200,
            },
            "execution": {
                "action": "BUY", "notional_usd": 250.0,
                "broker_hint": "webull", "derived_from_plan": True,
                "derived_at": "2026-02-22T15:30:00Z",
            },
        }
        out = normalize_intent(doc)
        assert out["plan"]["thesis"] == "high conviction breakout"
        assert out["plan"]["size_posture"] == "ELEVATED"
        assert out["execution"]["action"] == "BUY"
        assert out["execution"]["notional_usd"] == 250.0


class TestEquivalentV2V3ProduceSameShape:
    """The key acceptance criterion from PRD §7 Step 2: a v2 doc and
    its v3 equivalent must produce the same normalised shape so the
    funnel produces identical output for both."""

    def test_buy_v2_and_v3_match(self):
        v2 = {"intent_id": "a", "action": "BUY", "confidence": 0.7}
        v3 = {
            "intent_id": "a",
            "intent_version": "v3",
            "plan": {
                "stance": "BULLISH", "setup": "other",
                "intent": "ENTER", "execution_style": "MARKET_NOW",
                "confidence": 0.7,
            },
            "execution": {"action": "BUY", "derived_from_plan": False},
        }
        nv2 = normalize_intent(v2)
        nv3 = normalize_intent(v3)
        # The action that downstream consumers care about lines up.
        assert nv2["execution"]["action"] == nv3["execution"]["action"]
        assert nv2["plan"]["intent"] == nv3["plan"]["intent"]
        assert nv2["plan"]["stance"] == nv3["plan"]["stance"]
        assert nv2["plan"]["confidence"] == nv3["plan"]["confidence"]

    def test_hold_v2_and_v3_match(self):
        v2 = {"action": "HOLD", "confidence": 0.4}
        v3 = {
            "intent_version": "v3",
            "plan": {
                "stance": "NEUTRAL", "setup": "other",
                "intent": "WATCH", "execution_style": "MARKET_NOW",
                "confidence": 0.4,
            },
        }
        nv2 = normalize_intent(v2)
        nv3 = normalize_intent(v3)
        # The critical row in §6.2 — HOLD → null action.
        assert nv2["execution"]["action"] is None
        assert nv3["execution"]["action"] is None
        assert nv2["plan"]["intent"] == nv3["plan"]["intent"] == "WATCH"


class TestEdgeCases:
    def test_empty_doc_returns_unchanged(self):
        assert normalize_intent({}) == {}

    def test_none_input_returns_none(self):
        assert normalize_intent(None) is None

    def test_unknown_action_falls_back_to_watch(self):
        # Defensive: an action string we don't recognise (corrupt row)
        # gets the safe WATCH / NEUTRAL bucket so it doesn't crash.
        doc = {"action": "DOUBLEDOWN", "confidence": 0.5}
        out = normalize_intent(doc)
        assert out["plan"]["intent"] == "WATCH"
        assert out["plan"]["stance"] == "NEUTRAL"

    def test_missing_action_treated_as_hold(self):
        doc = {"confidence": 0.5}
        out = normalize_intent(doc)
        assert out["execution"]["action"] is None
        assert out["plan"]["intent"] == "WATCH"

    def test_normalize_intents_batch(self):
        rows = [
            {"action": "BUY", "confidence": 0.7},
            {"action": "HOLD", "confidence": 0.3},
            {"intent_version": "v3", "plan": {
                "stance": "BEARISH", "setup": "breakdown",
                "intent": "ENTER", "execution_style": "MARKET_NOW",
                "confidence": 0.6,
            }},
        ]
        out = normalize_intents(rows)
        assert len(out) == 3
        assert out[0]["execution"]["action"] == "BUY"
        assert out[1]["execution"]["action"] is None
        assert out[2]["plan"]["intent"] == "ENTER"

    def test_normalize_intents_empty(self):
        assert normalize_intents([]) == []
        assert normalize_intents(None) == []


# ─── Doctrine guardrail (PRD §6.1 rule 3) ─────────────────────────
class TestLifterDoctrine:
    def test_lifter_returns_new_dict(self):
        """The lifter must be pure — input is never touched. Required
        by PRD §6.1 'every read path lifts on read' (the lifter is
        called many times per request; mutating the source row would
        produce subtly-corrupt downstream behaviour)."""
        doc = {"action": "BUY", "confidence": 0.7}
        out = normalize_intent(doc)
        assert out is not doc

    def test_lifter_idempotent_for_v3(self):
        """Calling the lifter twice on a v3 doc yields the same result.
        Defensive: some routes lift in the projection helper AND again
        in the controller; the second call must be a no-op."""
        doc = {
            "intent_version": "v3",
            "plan": {
                "stance": "BULLISH", "setup": "bull_flag",
                "intent": "ENTER", "execution_style": "MARKET_NOW",
                "confidence": 0.7,
            },
        }
        once = normalize_intent(doc)
        twice = normalize_intent(once)
        assert once == twice
