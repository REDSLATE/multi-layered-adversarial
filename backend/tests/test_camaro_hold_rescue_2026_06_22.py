"""Camaro wrap — HOLD-rescue tape tie-breaker.

Locks the narrow-envelope carve-out shipped 2026-06-22:

    When the brain emits HOLD but the tape is decisive AND the
    brain's own confidence was at least moderate, the camaro wrap
    is permitted to promote the HOLD into a directional trade.

The other three wrappers (alpha, chevelle, redeye) remain bound by
the strict "no HOLD rescue" rule. This file pins the camaro carve-
out only.

Verification window: 4 weeks of `CAMARO_TAPE_OVERRIDE` trades have
to show positive expectancy or the carve-out gets removed. The
evidence stamp this file asserts on is what the verifier uses to
isolate those trades from the organic flow.
"""
from __future__ import annotations

import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, "/app/backend")

from shared.legacy_brain_wrappers import apply_camaro_legacy_doctrine


# ── helpers ──────────────────────────────────────────────────────


def _base_hold_intent(**overrides):
    """Default Barracuda HOLD intent shape. Tests override only the
    fields they're exercising — the rest stay at sensible defaults
    so the wrap doesn't trip on missing keys."""
    intent = {
        "brain_id": "barracuda",
        "display_name": "Barracuda",
        "action": "HOLD",
        "confidence": 0.59,
        "size_bias": 0.0,
        "current_side": "FLAT",
        "transition_intent": None,
        "position_evolution": None,
        "risk_transition": None,
        "reasons": [],
        "warnings": [],
        "evidence": {
            "buy_score": 0.62,
            "sell_score": 0.32,    # gap = 0.30, decisive long tape
            "market_regime": "calm_bull",
            "snapshot": {"squeeze": {"grade": "B"}},
        },
    }
    intent.update(overrides)
    return intent


# ── happy path: rescue fires ─────────────────────────────────────


def test_hold_rescue_fires_to_buy_on_decisive_long_tape():
    """Brain HOLD at 0.59 conf + tape gap 0.30 (buy>sell) → wrap
    rescues to BUY at the configured output confidence."""
    intent = _base_hold_intent(confidence=0.59)
    out = apply_camaro_legacy_doctrine(intent)

    assert out["action"] == "BUY", (
        f"Decisive long tape on a borderline HOLD must rescue to BUY. "
        f"Got action={out['action']!r} from out={out}"
    )
    # Output confidence pinned at the cap (0.68 default).
    assert out["confidence"] == pytest.approx(0.68, abs=0.05), (
        f"Rescued confidence must be at the configured cap. Got "
        f"{out['confidence']!r}"
    )
    assert "CAMARO_TAPE_OVERRIDE" in out["reasons"]
    # Provenance stamp — the verifier needs ALL of these to isolate
    # P&L attribution four weeks from now.
    ev = out["evidence"]["camaro_tape_override"]
    assert ev["fired"] is True
    assert ev["original_action"] == "HOLD"
    assert ev["original_confidence"] == pytest.approx(0.59, abs=0.05)
    assert ev["rescued_to"] == "BUY"
    assert ev["buy_score"] == pytest.approx(0.62, abs=1e-3)
    assert ev["sell_score"] == pytest.approx(0.32, abs=1e-3)
    assert ev["score_gap"] >= 0.25


def test_hold_rescue_fires_to_sell_on_decisive_short_tape():
    """Mirror case: tape decisively bearish → rescue to SELL."""
    intent = _base_hold_intent(
        confidence=0.60,
        evidence={
            "buy_score": 0.28,
            "sell_score": 0.58,    # gap = 0.30, decisive short tape
            "market_regime": "calm_bear",
            "snapshot": {"squeeze": {"grade": "B"}},
        },
    )
    out = apply_camaro_legacy_doctrine(intent)
    assert out["action"] == "SELL"
    assert "CAMARO_TAPE_OVERRIDE" in out["reasons"]
    assert out["evidence"]["camaro_tape_override"]["rescued_to"] == "SELL"


# ── guardrail: only HOLD can be rescued ──────────────────────────


def test_buy_action_never_rescued_or_flipped():
    """The carve-out is HOLD-only. A BUY at borderline confidence
    with bearish tape must NOT flip to SELL — the wrap's BUY↔SELL
    invariant is sacred."""
    intent = _base_hold_intent(
        action="BUY",
        confidence=0.59,
        evidence={
            "buy_score": 0.25,
            "sell_score": 0.55,    # tape disagrees with brain BUY
            "market_regime": "chop",
            "snapshot": {"squeeze": {"grade": "B"}},
        },
    )
    out = apply_camaro_legacy_doctrine(intent)
    assert out["action"] == "BUY", (
        f"BUY must NEVER flip to SELL via the rescue branch. "
        f"Got {out['action']!r}"
    )
    # No tape-override stamp on actions that weren't rescued.
    assert "CAMARO_TAPE_OVERRIDE" not in out.get("reasons", [])
    assert "camaro_tape_override" not in out["evidence"]


# ── guardrail: thresholds must both clear ────────────────────────


def test_hold_not_rescued_when_confidence_below_floor():
    """Confidence floor blocks the rescue even if tape is decisive."""
    intent = _base_hold_intent(confidence=0.45)  # below 0.55 floor
    out = apply_camaro_legacy_doctrine(intent)
    assert out["action"] == "HOLD"
    assert "CAMARO_TAPE_OVERRIDE" not in out.get("reasons", [])
    assert "camaro_tape_override" not in out["evidence"]


def test_hold_not_rescued_when_tape_gap_too_small():
    """Decisive-tape threshold blocks the rescue even if brain
    confidence cleared the floor."""
    intent = _base_hold_intent(
        confidence=0.60,
        evidence={
            "buy_score": 0.52,
            "sell_score": 0.42,    # gap = 0.10, below 0.25 minimum
            "market_regime": "chop",
            "snapshot": {"squeeze": {"grade": "B"}},
        },
    )
    out = apply_camaro_legacy_doctrine(intent)
    assert out["action"] == "HOLD"
    assert "CAMARO_TAPE_OVERRIDE" not in out.get("reasons", [])


# ── kill switch ──────────────────────────────────────────────────


def test_env_kill_switch_disables_rescue_entirely():
    """`CAMARO_TIEBREAK_HOLD_RESCUE_ENABLED=false` must disable
    the carve-out without code change. Operator's last-resort
    emergency dial."""
    intent = _base_hold_intent()
    with patch.dict(os.environ, {
        "CAMARO_TIEBREAK_HOLD_RESCUE_ENABLED": "false",
    }):
        out = apply_camaro_legacy_doctrine(intent)
    assert out["action"] == "HOLD", (
        "Kill switch must prevent rescue even when both thresholds "
        f"clear. Got {out['action']!r}"
    )
    assert "CAMARO_TAPE_OVERRIDE" not in out.get("reasons", [])


# ── env tunables ─────────────────────────────────────────────────


def test_env_threshold_tightening_blocks_rescue():
    """Operator tightens the tape-gap minimum past the available
    gap — rescue should be blocked."""
    intent = _base_hold_intent()  # default gap = 0.30
    with patch.dict(os.environ, {
        "CAMARO_TIEBREAK_HOLD_RESCUE_TAPE_GAP_MIN": "0.40",  # > 0.30
    }):
        out = apply_camaro_legacy_doctrine(intent)
    assert out["action"] == "HOLD"


def test_env_output_confidence_is_honoured():
    """The rescued confidence cap is env-tunable. Verify the wrap
    reads the override rather than using its default."""
    intent = _base_hold_intent()
    with patch.dict(os.environ, {
        "CAMARO_TIEBREAK_HOLD_RESCUE_OUTPUT_CONFIDENCE": "0.62",
    }):
        out = apply_camaro_legacy_doctrine(intent)
    assert out["action"] == "BUY"
    # Allow ±0.05 because other wrap branches may further nudge
    # confidence after the rescue stamps its base value (regime,
    # continuation, dampener). The point is the rescue STARTED from
    # 0.62 (the env-overridden cap), not the 0.68 default.
    assert out["confidence"] < 0.68, (
        f"Env override should have lowered the rescued confidence "
        f"below the 0.68 default. Got {out['confidence']!r}"
    )


# ── evidence isolation (the 4-week verifier needs this) ──────────


def test_evidence_stamp_carries_full_provenance_for_verifier():
    """The verifier four weeks from now MUST be able to:
      1. Filter for rescued trades by `evidence.camaro_tape_override.fired`
      2. Reconstruct the original brain verdict
      3. See the exact tape numbers that triggered the rescue
      4. See the thresholds that were active when it fired
    If any of these fields go missing, the verifier can't isolate
    rescued P&L from organic and the carve-out can't be evaluated."""
    intent = _base_hold_intent(confidence=0.59)
    out = apply_camaro_legacy_doctrine(intent)
    ev = out["evidence"]["camaro_tape_override"]
    required_keys = {
        "fired", "original_action", "original_confidence",
        "rescued_to", "rescued_confidence", "rescued_size_bias",
        "buy_score", "sell_score", "score_gap", "thresholds",
    }
    missing = required_keys - set(ev.keys())
    assert not missing, (
        f"Verifier requires all of {required_keys}. "
        f"Stamp missing: {missing}"
    )
    assert "confidence_floor" in ev["thresholds"]
    assert "tape_gap_min" in ev["thresholds"]
    # Also the lightweight summary on the legacy_wrapper block so
    # dashboards don't have to dig.
    assert out["evidence"]["legacy_wrapper"]["hold_rescue"] == "fired"


# ── doctrine: tape direction must be unambiguous ─────────────────


def test_equal_buy_sell_scores_does_not_rescue():
    """If buy_score == sell_score (no decisive tape direction even
    though abs gap might somehow exceed the minimum), the rescue
    must abstain — there's no signal to act on. score_gap is the
    absolute difference, so this also exercises the gap=0 path."""
    intent = _base_hold_intent(
        confidence=0.60,
        evidence={
            "buy_score": 0.50,
            "sell_score": 0.50,    # gap = 0.00
            "market_regime": "chop",
            "snapshot": {"squeeze": {"grade": "B"}},
        },
    )
    out = apply_camaro_legacy_doctrine(intent)
    assert out["action"] == "HOLD"
    assert "CAMARO_TAPE_OVERRIDE" not in out.get("reasons", [])
