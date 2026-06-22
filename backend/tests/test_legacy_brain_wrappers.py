"""Tests for the legacy brain wrapper layer.

Doctrine pin (operator directive, 2026-06-XX):

    new brain engine
        + old Alpha executor instincts on Camino
        + old Chevelle governor instincts on Hellcat
        + old Camaro tape-reading instincts on Barracuda
        + old RedEye adversary instincts on GTO
    without locking any of them into a seat

Final matrix:
    Camino    = trend          + Alpha executor discipline
    Barracuda = mean reversion + Camaro tape reading
    Hellcat   = breakout       + Chevelle risk compression
    GTO       = momentum       + RedEye adversary / opponent

A wrapper MUST NEVER:
    - flip action (BUY ↔ SELL)
    - create a trade from HOLD
    - force a seat

These tests pin those invariants.
"""
import sys

sys.path.insert(0, "/app/backend")

from shared.legacy_brain_wrappers import (  # noqa: E402
    BRAIN_WRAPPER_ASSIGNMENTS,
    apply_alpha_legacy_doctrine,
    apply_camaro_legacy_doctrine,
    apply_chevelle_legacy_doctrine,
    apply_legacy_wrapper,
    apply_redeye_legacy_doctrine,
    clamp,
    safe_float,
)


# ── Wrapper assignment registry ───────────────────────────────────


def test_camino_assigned_alpha_wrapper():
    assert BRAIN_WRAPPER_ASSIGNMENTS["camino"] == "alpha_legacy_doctrine"


def test_hellcat_assigned_chevelle_wrapper():
    assert BRAIN_WRAPPER_ASSIGNMENTS["hellcat"] == "chevelle_legacy_doctrine"


def test_barracuda_assigned_camaro_wrapper():
    """Barracuda's mean-reversion doctrine gets tape-reading
    instinct from the Camaro wrapper — prevents pure fading from
    fighting strong tape too aggressively."""
    assert BRAIN_WRAPPER_ASSIGNMENTS["barracuda"] == "camaro_legacy_doctrine"


def test_gto_assigned_redeye_wrapper():
    """GTO's momentum doctrine gets RedEye adversary instincts —
    challenges weak consensus, rewards short pressure in risk-off,
    punishes crowded long adds against bearish flow."""
    assert BRAIN_WRAPPER_ASSIGNMENTS["gto"] == "redeye_legacy_doctrine"


def test_apply_legacy_wrapper_passthrough_for_unassigned():
    """Brains without a wrapper assignment must pass through unchanged.
    After the RedEye-adversary addition all four brains carry a
    wrapper, so this verifies the passthrough still works for any
    truly-unknown brain id (e.g. a future brain or a typo)."""
    inp = {"brain_id": "no_such_brain", "action": "BUY", "confidence": 0.71}
    out = apply_legacy_wrapper(inp)
    assert out is inp  # literally the same dict


# ── Invariants the wrapper MUST honor ─────────────────────────────


def _base_intent(**overrides):
    base = {
        "brain_id": "camino",
        "display_name": "Camino",
        "action": "BUY",
        "confidence": 0.70,
        "size_bias": 1.0,
        "current_side": "LONG",
        "transition_intent": "ADD_LONG",
        "position_evolution": "ADD",
        "risk_transition": "NEUTRAL",
        "reasons": [],
        "warnings": [],
        "evidence": {},
    }
    base.update(overrides)
    return base


def test_alpha_wrapper_never_flips_action():
    out = apply_alpha_legacy_doctrine(_base_intent(action="BUY"))
    assert out["action"] == "BUY"
    out = apply_alpha_legacy_doctrine(_base_intent(action="SELL"))
    assert out["action"] == "SELL"


def test_chevelle_wrapper_never_flips_action():
    out = apply_chevelle_legacy_doctrine(_base_intent(action="BUY", brain_id="hellcat"))
    assert out["action"] == "BUY"
    out = apply_chevelle_legacy_doctrine(_base_intent(action="SELL", brain_id="hellcat"))
    assert out["action"] == "SELL"


def test_chevelle_wrapper_zeros_size_on_hold():
    out = apply_chevelle_legacy_doctrine(_base_intent(
        action="HOLD", brain_id="hellcat",
    ))
    assert out["action"] == "HOLD"
    assert out["size_bias"] == 0.0


def test_neither_wrapper_creates_a_trade_from_hold():
    """The wrapper must not promote HOLD to BUY/SELL."""
    a = apply_alpha_legacy_doctrine(_base_intent(action="HOLD"))
    c = apply_chevelle_legacy_doctrine(_base_intent(action="HOLD", brain_id="hellcat"))
    assert a["action"] == "HOLD"
    assert c["action"] == "HOLD"


# ── Alpha executor behavior ───────────────────────────────────────


def test_alpha_rewards_strong_open_long_commitment():
    """Clean ADD_LONG + confidence >= 0.68 → confidence lifted,
    size_bias boosted, reason logged."""
    out = apply_alpha_legacy_doctrine(_base_intent(
        confidence=0.70, transition_intent="ADD_LONG",
    ))
    assert out["confidence"] > 0.70
    assert out["size_bias"] > 1.0
    assert any("CLEAN_EXECUTION_COMMITMENT" in r for r in out["reasons"])


def test_alpha_penalizes_weak_open_commitment():
    out = apply_alpha_legacy_doctrine(_base_intent(
        confidence=0.55, transition_intent="OPEN_LONG",
    ))
    assert out["confidence"] < 0.55
    assert out["size_bias"] < 1.0
    assert any("WEAK_COMMITMENT_FOR_EXPOSURE_INCREASE" in w for w in out["warnings"])


def test_alpha_penalizes_unknown_position_state():
    """The AAPL-incident lesson: if current_side is unknown, Alpha
    instinct says compress and warn — don't act blind."""
    out = apply_alpha_legacy_doctrine(_base_intent(current_side=None))
    assert out["confidence"] < 0.70
    assert out["size_bias"] < 1.0
    assert any("POSITION_STATE_UNKNOWN" in w for w in out["warnings"])


def test_alpha_rewards_confirmed_scale_in():
    out = apply_alpha_legacy_doctrine(_base_intent(
        confidence=0.75, position_evolution="SCALE_IN",
    ))
    assert any("SCALE_IN_CONFIRMED" in r for r in out["reasons"])


def test_alpha_warns_on_unconfirmed_scale_in():
    out = apply_alpha_legacy_doctrine(_base_intent(
        confidence=0.65, position_evolution="SCALE_IN",
    ))
    assert any("SCALE_IN_NOT_CONFIRMED" in w for w in out["warnings"])


def test_alpha_compresses_flip_heavily():
    out = apply_alpha_legacy_doctrine(_base_intent(
        transition_intent="FLIP_LONG_TO_SHORT",
    ))
    assert out["size_bias"] <= 0.50
    assert any("FLIP_REQUIRES_STRONG_CONFIRMATION" in w for w in out["warnings"])


def test_alpha_stamps_provenance_block():
    out = apply_alpha_legacy_doctrine(_base_intent())
    lw = out["evidence"]["legacy_wrapper"]
    assert lw["name"] == "alpha_legacy_doctrine"
    assert lw["parent_brain"] == "alpha"
    assert out["wrapper"] == "alpha_legacy_doctrine"
    assert out["parent_brain"] == "alpha"


# ── Chevelle governor behavior ────────────────────────────────────


def test_chevelle_compresses_exposure_in_risk_off():
    """RISK_OFF + ADD_LONG → confidence dropped, size halved, warn."""
    out = apply_chevelle_legacy_doctrine(_base_intent(
        brain_id="hellcat", risk_transition="RISK_OFF",
        transition_intent="ADD_LONG",
    ))
    assert out["confidence"] < 0.70
    assert out["size_bias"] <= 0.50
    assert any("RISK_OFF_EXPOSURE_INCREASE_COMPRESSED" in w for w in out["warnings"])


def test_chevelle_approves_reductions_in_risk_off():
    out = apply_chevelle_legacy_doctrine(_base_intent(
        brain_id="hellcat", risk_transition="RISK_OFF",
        position_evolution="SCALE_OUT", transition_intent="REDUCE_LONG",
    ))
    assert out["confidence"] > 0.70
    assert any("RISK_OFF_REDUCTION_APPROVED" in r for r in out["reasons"])


def test_chevelle_allows_exposure_in_risk_on():
    out = apply_chevelle_legacy_doctrine(_base_intent(
        brain_id="hellcat", risk_transition="RISK_ON",
        transition_intent="OPEN_LONG",
    ))
    assert out["confidence"] >= 0.70
    assert any("RISK_ON_EXPOSURE_ALLOWED" in r for r in out["reasons"])


def test_chevelle_compresses_scale_in_size():
    """Hellcat-as-governor instinct: SCALE_IN gets size compressed
    even when conditions allow it. Risk discipline first."""
    out = apply_chevelle_legacy_doctrine(_base_intent(
        brain_id="hellcat", position_evolution="SCALE_IN",
    ))
    assert out["size_bias"] < 1.0
    assert any("SCALE_IN_SIZE_COMPRESSION" in w for w in out["warnings"])


def test_chevelle_heavily_compresses_flip():
    out = apply_chevelle_legacy_doctrine(_base_intent(
        brain_id="hellcat", transition_intent="FLIP_SHORT_TO_LONG",
    ))
    assert out["size_bias"] <= 0.35
    assert any("FLIP_HEAVILY_COMPRESSED" in w for w in out["warnings"])


def test_chevelle_penalizes_unknown_position_state():
    out = apply_chevelle_legacy_doctrine(_base_intent(
        brain_id="hellcat", current_side=None,
    ))
    assert out["confidence"] < 0.70
    assert any("POSITION_STATE_UNKNOWN" in w for w in out["warnings"])


# ── Confidence + size_bias always within bounds ───────────────────


def test_confidence_clamped_to_0_1():
    """Even with extreme inputs, confidence must stay in [0,1]."""
    out = apply_alpha_legacy_doctrine(_base_intent(confidence=2.5))
    assert 0.0 <= out["confidence"] <= 1.0
    out = apply_alpha_legacy_doctrine(_base_intent(confidence=-0.5))
    assert 0.0 <= out["confidence"] <= 1.0


def test_size_bias_clamped_to_0_2():
    """size_bias must stay in [0, 2.0]."""
    out = apply_chevelle_legacy_doctrine(_base_intent(
        brain_id="hellcat", size_bias=99.0,
    ))
    assert 0.0 <= out["size_bias"] <= 2.0


# ── Public helpers ────────────────────────────────────────────────


def test_clamp_helper():
    assert clamp(0.5) == 0.5
    assert clamp(1.5) == 1.0
    assert clamp(-1.0) == 0.0
    assert clamp(0.5, 0.0, 0.4) == 0.4


def test_safe_float_handles_garbage():
    assert safe_float("3.5") == 3.5
    assert safe_float(None, 0.7) == 0.7
    assert safe_float("abc", 0.5) == 0.5
    assert safe_float({}, -1.0) == -1.0


# ── Camaro strategist behavior ────────────────────────────────────


def _barracuda_intent(**overrides):
    base = {
        "brain_id": "barracuda",
        "display_name": "Barracuda",
        "action": "BUY",
        "confidence": 0.70,
        "size_bias": 1.0,
        "current_side": "FLAT",
        "transition_intent": "OPEN_LONG",
        "position_evolution": "OPEN",
        "risk_transition": "NEUTRAL",
        "reasons": [],
        "warnings": [],
        "evidence": {
            "market_regime": "bull",
            "buy_score": 0.72,
            "sell_score": 0.55,
        },
    }
    base.update(overrides)
    return base


def test_camaro_never_flips_action():
    out = apply_camaro_legacy_doctrine(_barracuda_intent(action="BUY"))
    assert out["action"] == "BUY"
    out = apply_camaro_legacy_doctrine(_barracuda_intent(action="SELL"))
    assert out["action"] == "SELL"


def test_camaro_zeros_size_on_hold():
    out = apply_camaro_legacy_doctrine(_barracuda_intent(action="HOLD"))
    assert out["action"] == "HOLD"
    assert out["size_bias"] == 0.0


def test_camaro_never_creates_trade_from_hold():
    out = apply_camaro_legacy_doctrine(_barracuda_intent(action="HOLD"))
    assert out["action"] == "HOLD"


def test_camaro_penalizes_tiny_score_gap_chop():
    """Camaro hates indecision — a tiny BUY/SELL score gap triggers
    chop warning and compresses size."""
    out = apply_camaro_legacy_doctrine(_barracuda_intent(
        evidence={
            "market_regime": "bull",
            "buy_score": 0.700,
            "sell_score": 0.690,   # gap = 0.010, under 0.035 floor
        },
    ))
    assert out["size_bias"] < 1.0
    assert any("TINY_SCORE_GAP_CHOP_RISK" in w for w in out["warnings"])


def test_camaro_rewards_long_continuation_in_bull_regime():
    """Bull regime + BUY OPEN_LONG → confidence lifted, size boosted."""
    out = apply_camaro_legacy_doctrine(_barracuda_intent(
        action="BUY", transition_intent="OPEN_LONG",
        evidence={"market_regime": "bull", "buy_score": 0.75, "sell_score": 0.50},
    ))
    assert out["confidence"] > 0.70
    assert out["size_bias"] > 1.0
    assert any("BULL_REGIME_LONG_CONTINUATION" in r for r in out["reasons"])


def test_camaro_warns_on_short_against_bull():
    """SELL against a bull regime — Camaro's experience says don't
    fight the tape, even for a mean-reverter."""
    out = apply_camaro_legacy_doctrine(_barracuda_intent(
        action="SELL", transition_intent="OPEN_SHORT",
        current_side="FLAT",
        evidence={"market_regime": "bull", "buy_score": 0.40, "sell_score": 0.65},
    ))
    assert out["confidence"] < 0.70
    assert out["size_bias"] < 1.0
    assert any("SHORT_AGAINST_BULL_REGIME" in w for w in out["warnings"])


def test_camaro_rewards_short_continuation_in_bear_regime():
    out = apply_camaro_legacy_doctrine(_barracuda_intent(
        action="SELL", transition_intent="OPEN_SHORT",
        current_side="FLAT",
        evidence={"market_regime": "bear", "buy_score": 0.40, "sell_score": 0.72},
    ))
    assert out["confidence"] > 0.70
    assert any("BEAR_REGIME_SHORT_CONTINUATION" in r for r in out["reasons"])


def test_camaro_warns_on_long_against_bear():
    out = apply_camaro_legacy_doctrine(_barracuda_intent(
        action="BUY", transition_intent="OPEN_LONG",
        evidence={"market_regime": "crisis", "buy_score": 0.72, "sell_score": 0.45},
    ))
    assert out["confidence"] < 0.70
    assert any("LONG_AGAINST_BEAR_REGIME" in w for w in out["warnings"])


def test_camaro_compresses_exposure_in_chop():
    """Chop / sideways / unknown regime → exposure-increasing
    transitions are compressed."""
    for regime in ("chop", "sideways", "unknown"):
        out = apply_camaro_legacy_doctrine(_barracuda_intent(
            transition_intent="OPEN_LONG",
            evidence={"market_regime": regime, "buy_score": 0.70, "sell_score": 0.50},
        ))
        assert out["size_bias"] < 1.0, f"regime={regime} should compress"
        assert any("CHOP_EXPOSURE_COMPRESSION" in w for w in out["warnings"])


def test_camaro_approves_position_management():
    """SCALE_OUT / PARTIAL_COVER / FULL_COVER → small confidence
    lift, reason logged."""
    out = apply_camaro_legacy_doctrine(_barracuda_intent(
        position_evolution="SCALE_OUT",
        evidence={"market_regime": "bull", "buy_score": 0.65, "sell_score": 0.60},
    ))
    assert any("POSITION_MANAGEMENT_APPROVED" in r for r in out["reasons"])


def test_camaro_rejects_low_confidence_flip_by_temperament():
    out = apply_camaro_legacy_doctrine(_barracuda_intent(
        confidence=0.60, transition_intent="FLIP_LONG_TO_SHORT",
    ))
    assert out["confidence"] < 0.60
    assert out["size_bias"] < 0.5
    assert any(
        "LOW_CONFIDENCE_FLIP_REJECTED_BY_TEMPERAMENT" in w
        for w in out["warnings"]
    )


def test_camaro_compresses_high_confidence_flip():
    """Even at high confidence, Camaro shrinks size on flips."""
    out = apply_camaro_legacy_doctrine(_barracuda_intent(
        confidence=0.80, transition_intent="FLIP_SHORT_TO_LONG",
    ))
    # Confidence preserved or lifted; size compressed.
    assert out["size_bias"] < 1.0
    assert any("HIGH_CONFIDENCE_FLIP_COMPRESSED" in w for w in out["warnings"])


def test_camaro_rewards_confirmed_long_continuation():
    """LONG + ADD_LONG + confidence >= 0.68 → continuation reward."""
    out = apply_camaro_legacy_doctrine(_barracuda_intent(
        confidence=0.72, current_side="LONG", transition_intent="ADD_LONG",
        evidence={"market_regime": "bull", "buy_score": 0.72, "sell_score": 0.45},
    ))
    assert any("LONG_CONTINUATION_CONFIRMED" in r for r in out["reasons"])


def test_camaro_rewards_confirmed_short_continuation():
    out = apply_camaro_legacy_doctrine(_barracuda_intent(
        confidence=0.72, current_side="SHORT", transition_intent="ADD_SHORT",
        action="SELL",
        evidence={"market_regime": "bear", "buy_score": 0.40, "sell_score": 0.72},
    ))
    assert any("SHORT_CONTINUATION_CONFIRMED" in r for r in out["reasons"])


def test_camaro_stamps_provenance_block():
    out = apply_camaro_legacy_doctrine(_barracuda_intent())
    lw = out["evidence"]["legacy_wrapper"]
    assert lw["name"] == "camaro_legacy_doctrine"
    assert lw["parent_brain"] == "camaro"
    assert out["wrapper"] == "camaro_legacy_doctrine"
    assert out["parent_brain"] == "camaro"


def test_camaro_clamps_confidence_and_size():
    """Bound invariants hold for Camaro too."""
    out = apply_camaro_legacy_doctrine(_barracuda_intent(
        confidence=5.0, size_bias=10.0,
    ))
    assert 0.0 <= out["confidence"] <= 1.0
    assert 0.0 <= out["size_bias"] <= 2.0


def test_camaro_handles_missing_market_regime_as_chop():
    """If market_regime is missing entirely, Camaro treats it as
    unclear and compresses exposure — fail-closed default."""
    out = apply_camaro_legacy_doctrine(_barracuda_intent(
        transition_intent="OPEN_LONG",
        evidence={"buy_score": 0.70, "sell_score": 0.50},  # no market_regime
    ))
    assert out["size_bias"] < 1.0
    assert any("CHOP_EXPOSURE_COMPRESSION" in w for w in out["warnings"])


def test_apply_legacy_wrapper_routes_barracuda_to_camaro():
    """End-to-end: barracuda intent through the generic dispatcher
    must land on the Camaro wrapper, not pass through unchanged."""
    out = apply_legacy_wrapper({
        "brain_id": "barracuda",
        "action": "BUY",
        "confidence": 0.70,
        "current_side": "FLAT",
        "transition_intent": "OPEN_LONG",
        "evidence": {"market_regime": "bull", "buy_score": 0.72, "sell_score": 0.50},
    })
    assert out.get("wrapper") == "camaro_legacy_doctrine"


# ── RedEye adversary behavior ─────────────────────────────────────


def _gto_intent(**overrides):
    base = {
        "brain_id": "gto",
        "display_name": "GTO",
        "action": "SELL",
        "confidence": 0.70,
        "size_bias": 1.0,
        "current_side": "FLAT",
        "transition_intent": "OPEN_SHORT",
        "position_evolution": "OPEN",
        "risk_transition": "NEUTRAL",
        "reasons": [],
        "warnings": [],
        "evidence": {
            "market_regime": "bear",
            "buy_score": 0.45,
            "sell_score": 0.72,
            "flow_imbalance": -0.30,
            "news_zscore": 1.0,
        },
    }
    base.update(overrides)
    return base


def test_redeye_never_flips_action():
    """RedEye wrapper preserves BUY/SELL exactly — adversarial bias
    lives in confidence + size, never in flipping the direction."""
    out = apply_redeye_legacy_doctrine(_gto_intent(action="BUY"))
    assert out["action"] == "BUY"
    out = apply_redeye_legacy_doctrine(_gto_intent(action="SELL"))
    assert out["action"] == "SELL"


def test_redeye_zeros_size_on_hold():
    out = apply_redeye_legacy_doctrine(_gto_intent(action="HOLD"))
    assert out["action"] == "HOLD"
    assert out["size_bias"] == 0.0


def test_redeye_never_creates_trade_from_hold():
    out = apply_redeye_legacy_doctrine(_gto_intent(action="HOLD"))
    assert out["action"] == "HOLD"


def test_redeye_challenges_weak_consensus():
    """Score gap < 0.04 → confidence dropped, size compressed,
    challenge warning logged. RedEye distrusts crowd indecision."""
    out = apply_redeye_legacy_doctrine(_gto_intent(
        evidence={
            "market_regime": "calm",
            "buy_score": 0.700,
            "sell_score": 0.690,  # gap = 0.010
            "flow_imbalance": 0.0,
            "news_zscore": 0.0,
        },
    ))
    assert out["confidence"] < 0.70
    assert out["size_bias"] < 1.0
    assert any("WEAK_CONSENSUS_CHALLENGED" in w for w in out["warnings"])


def test_redeye_compresses_long_against_risk_off():
    """BUY while RISK_OFF → heavy compression. The 2026-06-09
    AAPL-style trap pattern is exactly this: brain wants to add
    long into a bid-failure tape."""
    out = apply_redeye_legacy_doctrine(_gto_intent(
        action="BUY", transition_intent="OPEN_LONG",
        risk_transition="RISK_OFF",
        evidence={
            "market_regime": "calm", "buy_score": 0.70, "sell_score": 0.55,
            "flow_imbalance": 0.0, "news_zscore": 0.0,
        },
    ))
    assert out["confidence"] < 0.70
    assert out["size_bias"] <= 0.45
    assert any("LONG_AGAINST_RISK_OFF_COMPRESSED" in w for w in out["warnings"])


def test_redeye_rewards_short_pressure_in_bear_regime():
    """SELL OPEN_SHORT in bear regime → confidence lifted, size
    boosted, confirmation reason logged."""
    out = apply_redeye_legacy_doctrine(_gto_intent(
        action="SELL", transition_intent="OPEN_SHORT",
        evidence={
            "market_regime": "bear", "buy_score": 0.40, "sell_score": 0.72,
            "flow_imbalance": -0.10, "news_zscore": 0.0,
        },
    ))
    assert out["confidence"] > 0.70
    assert out["size_bias"] > 1.0
    assert any("SHORT_PRESSURE_CONFIRMED" in r for r in out["reasons"])


def test_redeye_rewards_short_continuation_with_real_confidence():
    """current_side=SHORT + ADD_SHORT + confidence >= 0.66 →
    continuation reward."""
    out = apply_redeye_legacy_doctrine(_gto_intent(
        action="SELL", current_side="SHORT",
        transition_intent="ADD_SHORT", confidence=0.72,
        evidence={
            "market_regime": "calm", "buy_score": 0.40, "sell_score": 0.72,
            "flow_imbalance": 0.0, "news_zscore": 0.0,
        },
    ))
    assert any("SHORT_CONTINUATION" in r for r in out["reasons"])


def test_redeye_compresses_weak_short_add():
    """current_side=SHORT + ADD_SHORT + confidence < 0.66 →
    weak add compression."""
    out = apply_redeye_legacy_doctrine(_gto_intent(
        action="SELL", current_side="SHORT",
        transition_intent="ADD_SHORT", confidence=0.55,
        evidence={
            "market_regime": "calm", "buy_score": 0.40, "sell_score": 0.55,
            "flow_imbalance": 0.0, "news_zscore": 0.0,
        },
    ))
    assert out["confidence"] < 0.55
    assert out["size_bias"] < 1.0
    assert any("WEAK_SHORT_ADD_COMPRESSED" in w for w in out["warnings"])


def test_redeye_warns_on_early_cover_during_downside():
    """current_side=SHORT + cover during bearish flow → don't cover
    too early, the pressure is still on."""
    out = apply_redeye_legacy_doctrine(_gto_intent(
        action="BUY", current_side="SHORT",
        position_evolution="PARTIAL_COVER",
        transition_intent="REDUCE_SHORT",
        evidence={
            "market_regime": "bear", "buy_score": 0.40, "sell_score": 0.70,
            "flow_imbalance": -0.40, "news_zscore": 0.0,
        },
    ))
    assert any("EARLY_COVER_WARNING" in w for w in out["warnings"])


def test_redeye_punishes_long_adds_against_bearish_flow():
    """BUY ADD_LONG with flow_imbalance < -0.20 → compression."""
    out = apply_redeye_legacy_doctrine(_gto_intent(
        action="BUY", current_side="LONG",
        transition_intent="ADD_LONG",
        evidence={
            "market_regime": "calm", "buy_score": 0.70, "sell_score": 0.55,
            "flow_imbalance": -0.30, "news_zscore": 0.0,
        },
    ))
    assert out["confidence"] < 0.70
    assert out["size_bias"] < 1.0
    assert any("BEARISH_FLOW_LONG_COMPRESSION" in w for w in out["warnings"])


def test_redeye_supports_sell_on_bearish_news_shock():
    """High news_zscore + bearish sentiment + SELL → confidence
    nudge, support reason logged."""
    out = apply_redeye_legacy_doctrine(_gto_intent(
        action="SELL", transition_intent="OPEN_SHORT",
        evidence={
            "market_regime": "calm", "buy_score": 0.45, "sell_score": 0.70,
            "flow_imbalance": -0.10,
            "news_zscore": 3.0, "sentiment_label": "bearish",
        },
    ))
    assert any("BEARISH_NEWS_SHOCK_SUPPORT" in r for r in out["reasons"])


def test_redeye_compresses_buy_against_bearish_news_shock():
    """High news_zscore + bearish sentiment + BUY → confidence
    dropped, size compressed, warning logged."""
    out = apply_redeye_legacy_doctrine(_gto_intent(
        action="BUY", current_side="FLAT", transition_intent="OPEN_LONG",
        evidence={
            "market_regime": "calm", "buy_score": 0.70, "sell_score": 0.50,
            "flow_imbalance": 0.0,
            "news_zscore": 3.0, "sentiment_label": "bearish",
        },
    ))
    assert out["confidence"] < 0.70
    assert out["size_bias"] < 1.0
    assert any("BEARISH_NEWS_SHOCK_AGAINST_LONG" in w for w in out["warnings"])


def test_redeye_compresses_low_confidence_flip():
    out = apply_redeye_legacy_doctrine(_gto_intent(
        confidence=0.55, transition_intent="FLIP_LONG_TO_SHORT",
    ))
    assert out["confidence"] < 0.55
    assert out["size_bias"] <= 0.45
    assert any("LOW_CONFIDENCE_FLIP_COMPRESSED" in w for w in out["warnings"])


def test_redeye_allows_high_confidence_flip_with_compression():
    """High-conf flips are allowed but size is still trimmed —
    RedEye is adversarial, not reckless."""
    out = apply_redeye_legacy_doctrine(_gto_intent(
        confidence=0.82, transition_intent="FLIP_SHORT_TO_LONG",
    ))
    assert out["size_bias"] < 1.0
    assert any("HIGH_CONFIDENCE_FLIP_ALLOWED_COMPRESSED" in r for r in out["reasons"])


def test_redeye_stamps_provenance_block():
    out = apply_redeye_legacy_doctrine(_gto_intent())
    lw = out["evidence"]["legacy_wrapper"]
    assert lw["name"] == "redeye_legacy_doctrine"
    assert lw["parent_brain"] == "redeye"
    assert lw["effect"] == "adversarial_short_pressure_and_consensus_challenge"
    assert out["wrapper"] == "redeye_legacy_doctrine"
    assert out["parent_brain"] == "redeye"
    assert out["doctrine"] == "opponent_adversary"


def test_redeye_clamps_confidence_and_size():
    """Bound invariants hold for RedEye too."""
    out = apply_redeye_legacy_doctrine(_gto_intent(
        confidence=5.0, size_bias=10.0,
    ))
    assert 0.0 <= out["confidence"] <= 1.0
    assert 0.0 <= out["size_bias"] <= 2.0


def test_apply_legacy_wrapper_routes_gto_to_redeye():
    """End-to-end: gto intent through the generic dispatcher must
    land on the RedEye adversary wrapper, not pass through unchanged."""
    out = apply_legacy_wrapper({
        "brain_id": "gto",
        "action": "SELL",
        "confidence": 0.70,
        "current_side": "FLAT",
        "transition_intent": "OPEN_SHORT",
        "evidence": {
            "market_regime": "bear",
            "buy_score": 0.40, "sell_score": 0.72,
            "flow_imbalance": -0.30,
        },
    })
    assert out.get("wrapper") == "redeye_legacy_doctrine"
    assert out.get("parent_brain") == "redeye"
