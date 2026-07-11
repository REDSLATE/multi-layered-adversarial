"""DAWE math — EWMA convergence, bounds, cold-start, quality mapping."""
from __future__ import annotations

import math

import pytest

from mc_arbiter.dawe import (
    ALPHA_RECENT,
    ALPHA_SESSION,
    COLD_START_GRADES,
    EXPONENT_PRIOR,
    EXPONENT_RECENT,
    EXPONENT_SESSION,
    WEIGHT_MAX,
    WEIGHT_MIN,
    compute_effective_weight,
    ewma,
    quality_from_signed_return,
    size_multiplier,
    update_recent,
    update_session,
)
from mc_arbiter.models import DaweState


# ── ewma ─────────────────────────────────────────────────────────

def test_ewma_alpha_1_returns_observation():
    assert ewma(previous=0.5, observation=1.2, alpha=1.0) == 1.2


def test_ewma_alpha_0_returns_previous():
    assert ewma(previous=0.5, observation=1.2, alpha=0.0) == 0.5


def test_ewma_alpha_0_5_averages():
    assert ewma(previous=0.5, observation=1.5, alpha=0.5) == pytest.approx(1.0)


def test_ewma_rejects_bad_alpha():
    with pytest.raises(ValueError):
        ewma(0.5, 1.0, alpha=1.5)
    with pytest.raises(ValueError):
        ewma(0.5, 1.0, alpha=-0.1)


def test_ewma_converges_to_observation():
    # Feed the same observation many times → EWMA converges to it.
    prev = 0.5
    for _ in range(50):
        prev = ewma(prev, 1.2, alpha=0.30)
    assert prev == pytest.approx(1.2, abs=1e-3)


# ── compute_effective_weight ─────────────────────────────────────

def _state(**overrides):
    base = dict(brain="camino", lane="equity")
    base.update(overrides)
    return DaweState(**base)


def test_effective_neutral_at_default_state():
    # All 1.0s → effective 1.0 (but cold-start guard fires first
    # because grades_used_session=0). Bump grades to skip guard.
    s = _state(grades_used_session=10)
    assert compute_effective_weight(s) == pytest.approx(1.0)


def test_effective_cold_start_returns_neutral():
    # Even with wild weights, if grades_used_session is below
    # COLD_START_GRADES the arm reads neutral. Thin data must not
    # move the arm.
    s = _state(
        session_weight=1.35, recent_weight=1.35, prior_weight=1.35,
        grades_used_session=COLD_START_GRADES - 1,
    )
    assert compute_effective_weight(s) == 1.0


def test_effective_active_after_cold_start_threshold():
    s = _state(
        session_weight=1.20, recent_weight=1.10, prior_weight=1.00,
        grades_used_session=COLD_START_GRADES,
    )
    expected = (
        1.20 ** EXPONENT_SESSION
        * 1.10 ** EXPONENT_RECENT
        * 1.00 ** EXPONENT_PRIOR
    )
    assert compute_effective_weight(s) == pytest.approx(expected, rel=1e-6)


def test_effective_clamped_to_min():
    s = _state(
        session_weight=0.10, recent_weight=0.10, prior_weight=0.10,
        grades_used_session=100,
    )
    assert compute_effective_weight(s) == WEIGHT_MIN


def test_effective_clamped_to_max():
    s = _state(
        session_weight=5.0, recent_weight=5.0, prior_weight=5.0,
        grades_used_session=100,
    )
    assert compute_effective_weight(s) == WEIGHT_MAX


def test_effective_survives_zero_weight():
    # A malformed doc that landed a zero weight must not crash the
    # arbiter. Should clamp to WEIGHT_MIN via the 1e-6 guard.
    s = _state(
        session_weight=0.0, recent_weight=1.0, prior_weight=1.0,
        grades_used_session=100,
    )
    result = compute_effective_weight(s)
    assert result == WEIGHT_MIN


def test_effective_session_dominates_recent():
    # Given exponents 0.50/0.30/0.20 a large session shift moves
    # effective_weight MORE than an equal-magnitude recent shift.
    s_session = _state(
        session_weight=1.30, recent_weight=1.00, prior_weight=1.00,
        grades_used_session=100,
    )
    s_recent = _state(
        session_weight=1.00, recent_weight=1.30, prior_weight=1.00,
        grades_used_session=100,
    )
    assert compute_effective_weight(s_session) > compute_effective_weight(s_recent)


# ── size_multiplier ──────────────────────────────────────────────

def test_size_multiplier_softens_weight_damage():
    # A weight of 0.49 (near floor) gives 0.70× sizing, not 0.49×.
    assert size_multiplier(0.49) == pytest.approx(0.70)
    assert size_multiplier(1.0) == 1.0
    # Above 1.0, sqrt shrinks the upside modestly too.
    assert size_multiplier(1.40) == pytest.approx(math.sqrt(1.40))


def test_size_multiplier_handles_zero():
    # Defensive against a manual DB edit that produced zero.
    assert size_multiplier(0.0) == 0.0
    assert size_multiplier(-0.1) == 0.0  # negative clamped


# ── update_session / update_recent ───────────────────────────────

def test_update_session_moves_toward_quality():
    # Quality 1.0 maps to contribution 1.40 (top of band). One EWMA
    # step with α=0.30 from prev=1.0 → 0.30 * 1.40 + 0.70 * 1.00 = 1.12.
    result = update_session(prev_weight=1.00, observed_quality=1.0)
    assert result == pytest.approx(0.30 * 1.40 + 0.70 * 1.00, rel=1e-6)
    assert result == pytest.approx(1.12, rel=1e-6)


def test_update_session_penalizes_bad_grade():
    # Quality 0.0 maps to contribution 0.40 (bottom). One step from
    # prev=1.0 → 0.30 * 0.40 + 0.70 * 1.00 = 0.82.
    result = update_session(prev_weight=1.00, observed_quality=0.0)
    assert result == pytest.approx(0.82, rel=1e-6)


def test_update_session_stays_in_bounds():
    # Feed max-quality repeatedly → converges toward WEIGHT_MAX,
    # never above.
    w = 1.0
    for _ in range(100):
        w = update_session(w, observed_quality=1.0)
    assert w <= WEIGHT_MAX
    assert w == pytest.approx(WEIGHT_MAX, abs=1e-3)


def test_update_session_stays_in_bounds_down():
    w = 1.0
    for _ in range(100):
        w = update_session(w, observed_quality=0.0)
    assert w >= WEIGHT_MIN
    assert w == pytest.approx(WEIGHT_MIN, abs=1e-3)


def test_update_recent_convergence_is_slower_than_session():
    # After 10 identical observations, recent should be closer to
    # its start than session — that's the point of the smaller α.
    obs = 1.30
    session_w = 1.0
    recent_w = 1.0
    for _ in range(10):
        session_w = ewma(session_w, obs, ALPHA_SESSION)
        recent_w = ewma(recent_w, obs, ALPHA_RECENT)
    # session has traveled further from 1.0 than recent.
    assert abs(session_w - 1.0) > abs(recent_w - 1.0)


# ── quality_from_signed_return ───────────────────────────────────

def test_quality_neutral_when_no_move():
    assert quality_from_signed_return(0.0, expected_move=0.01) == 0.5


def test_quality_perfect_when_captures_expected_move():
    # Return exactly matches expected_move in the right direction →
    # quality = 1.0.
    assert quality_from_signed_return(
        signed_return=0.010, expected_move=0.010,
    ) == pytest.approx(1.0)


def test_quality_zero_when_wrong_direction_captures_expected_move():
    assert quality_from_signed_return(
        signed_return=-0.010, expected_move=0.010,
    ) == pytest.approx(0.0)


def test_quality_clamped_when_move_exceeds_expected():
    # A 3× expected move in the right direction still caps at 1.0.
    assert quality_from_signed_return(
        signed_return=0.030, expected_move=0.010,
    ) == 1.0


def test_quality_handles_zero_expected_move():
    # Divide-by-zero guard — should not crash, must return either
    # 0.0 or 1.0 depending on sign.
    result = quality_from_signed_return(
        signed_return=0.001, expected_move=0.0,
    )
    assert 0.0 <= result <= 1.0
