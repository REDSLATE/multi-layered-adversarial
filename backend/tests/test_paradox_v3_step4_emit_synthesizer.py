"""Paradox v3 — Step 4 v3 emit synthesizer + brain-enable flag.

Pins:
  * `v3_brain_enabled` respects the comma-separated env list.
  * `synthesize_v3_envelope` upgrades a v2 payload losslessly:
    running it through `normalize_intent` produces a structurally
    identical v3 doc (the round-trip property that makes Step 4 safe).
  * Synthesized envelope flags `execution.derived_from_plan=True`
    (NOT False) to distinguish v3-aware emits from legacy fast-path
    rows lifted on read.
"""
from __future__ import annotations

import pytest

from shared.intent_envelope_v3 import (
    normalize_intent,
    synthesize_v3_envelope,
    v3_brain_enabled,
)


# ── Brain-enable flag ─────────────────────────────────────────────
class TestV3BrainEnabled:
    @pytest.fixture(autouse=True)
    def _clear(self, monkeypatch):
        monkeypatch.delenv("PARADOX_V3_BRAINS", raising=False)
        yield

    def test_default_off(self):
        assert v3_brain_enabled("camino") is False
        assert v3_brain_enabled("barracuda") is False

    def test_single_brain(self, monkeypatch):
        monkeypatch.setenv("PARADOX_V3_BRAINS", "camino")
        assert v3_brain_enabled("camino") is True
        assert v3_brain_enabled("barracuda") is False

    def test_csv_list(self, monkeypatch):
        monkeypatch.setenv("PARADOX_V3_BRAINS", "camino,barracuda")
        assert v3_brain_enabled("camino") is True
        assert v3_brain_enabled("barracuda") is True
        assert v3_brain_enabled("hellcat") is False

    def test_whitespace_tolerance(self, monkeypatch):
        monkeypatch.setenv("PARADOX_V3_BRAINS", "  camino , barracuda  ")
        assert v3_brain_enabled("camino") is True
        assert v3_brain_enabled("barracuda") is True

    def test_case_insensitive(self, monkeypatch):
        monkeypatch.setenv("PARADOX_V3_BRAINS", "CAMINO")
        assert v3_brain_enabled("camino") is True
        assert v3_brain_enabled("Camino") is True

    def test_custom_env_var(self, monkeypatch):
        monkeypatch.setenv("MY_FLAG", "barracuda")
        assert v3_brain_enabled("barracuda", env_var="MY_FLAG") is True
        assert v3_brain_enabled("barracuda") is False


# ── Synthesizer correctness ───────────────────────────────────────
class TestSynthesizer:
    def test_buy_payload_upgrades_to_v3(self):
        payload = {
            "stack": "camino", "symbol": "AAPL", "lane": "equity",
            "action": "BUY", "confidence": 0.72, "rationale": "above_vwap",
        }
        out = synthesize_v3_envelope(payload)
        assert out["intent_version"] == "v3"
        assert out["plan"]["stance"] == "BULLISH"
        assert out["plan"]["intent"] == "ENTER"
        assert out["plan"]["execution_style"] == "MARKET_NOW"
        assert out["plan"]["confidence"] == 0.72
        assert out["plan"]["thesis"] == "above_vwap"
        assert out["execution"]["action"] == "BUY"
        # CRITICAL — v3-aware emit, not legacy fast-path.
        assert out["execution"]["derived_from_plan"] is True
        # Legacy v2 fields preserved.
        assert out["action"] == "BUY"
        assert out["confidence"] == 0.72

    def test_hold_synthesizes_null_action(self):
        payload = {"stack": "camino", "action": "HOLD", "confidence": 0.3, "rationale": "wait"}
        out = synthesize_v3_envelope(payload)
        assert out["plan"]["intent"] == "WATCH"
        assert out["plan"]["stance"] == "NEUTRAL"
        assert out["execution"]["action"] is None

    def test_sell_synthesizes_exit_bearish(self):
        payload = {"stack": "camino", "action": "SELL", "confidence": 0.6, "rationale": "x"}
        out = synthesize_v3_envelope(payload)
        assert out["plan"]["stance"] == "BEARISH"
        assert out["plan"]["intent"] == "EXIT"
        assert out["execution"]["action"] == "SELL"

    def test_target_and_stop_lift_into_plan(self):
        payload = {
            "stack": "camino", "action": "BUY", "confidence": 0.7,
            "rationale": "test", "target_price": 105.0, "stop_price": 95.0,
        }
        out = synthesize_v3_envelope(payload)
        assert out["plan"]["target_prices"] == [105.0]
        assert out["plan"]["invalidation_price"] == 95.0

    def test_input_payload_not_mutated(self):
        payload = {"stack": "camino", "action": "BUY", "confidence": 0.7}
        synthesize_v3_envelope(payload)
        assert "intent_version" not in payload
        assert "plan" not in payload
        assert "execution" not in payload

    def test_empty_payload_passes_through(self):
        assert synthesize_v3_envelope({}) == {}
        assert synthesize_v3_envelope(None) is None


# ── Round-trip property ───────────────────────────────────────────
class TestRoundTrip:
    """The synthesizer + lifter are mirror operations. Running them
    in sequence must produce a stable shape — this is the property
    that lets us flip camino on v3 without breaking downstream."""

    @pytest.mark.parametrize("action", ["BUY", "SELL", "SHORT", "COVER", "HOLD"])
    def test_synthesize_then_normalize_is_stable(self, action):
        v2 = {
            "stack": "camino", "symbol": "X", "lane": "equity",
            "action": action, "confidence": 0.6, "rationale": "test",
        }
        v3 = synthesize_v3_envelope(v2)
        lifted = normalize_intent(v3)
        # The lifter must NOT regress the synthesizer's classification.
        assert lifted["plan"]["intent"] == v3["plan"]["intent"]
        assert lifted["plan"]["stance"] == v3["plan"]["stance"]
        assert lifted["execution"]["action"] == v3["execution"]["action"]

    def test_v2_emit_vs_v3_emit_produce_same_lifted_shape(self):
        """A camino emit at v2 vs the same emit at v3 should produce
        the same shape on the read side (so the post-mortem can't tell
        them apart for purely-fast-path intents)."""
        v2_payload = {
            "stack": "camino", "symbol": "AAPL", "lane": "equity",
            "action": "BUY", "confidence": 0.7, "rationale": "test",
        }
        # Simulate v2 ingest (no intent_version, no plan, no execution).
        v2_persisted = dict(v2_payload, intent_version="v2", plan=None, execution=None)
        v3_persisted = synthesize_v3_envelope(v2_payload)

        n_v2 = normalize_intent(v2_persisted)
        n_v3 = normalize_intent(v3_persisted)
        # Every operator-facing classification is identical.
        assert n_v2["plan"]["intent"] == n_v3["plan"]["intent"]
        assert n_v2["plan"]["stance"] == n_v3["plan"]["stance"]
        assert n_v2["execution"]["action"] == n_v3["execution"]["action"]
        # The ONE field that legitimately differs — derived_from_plan
        # distinguishes "legacy fast-path lift" from "v3-aware emit".
        assert n_v2["execution"]["derived_from_plan"] is False
        assert n_v3["execution"]["derived_from_plan"] is True
