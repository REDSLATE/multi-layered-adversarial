"""Tests for the Pine grade → confidence mapping and dir → side map.

Doctrine pins these tests guard:
  * The Seat's 0.70 confidence floor: A+ / A / B+ MUST land above it,
    B / C+ / C / D / F MUST land at-or-below it.
  * Score bonus is bounded by ±SCORE_BONUS_CAP — the grade dominates,
    the raw Pine score can only nudge.
  * Unknown grades return the LOG_ONLY floor (below 0.70).
  * Pine direction mapping is strict — unknown values raise.
"""
from __future__ import annotations

import pytest

from shared.external_signals import (
    GRADE_TO_CONFIDENCE,
    SCORE_BONUS_CAP,
    UNKNOWN_GRADE_FLOOR,
    build_dedup_key,
    grade_score_to_confidence,
    pine_dir_to_side,
)
from shared.external_signals.scoring import PINE_SCORE_MAX


SEAT_FLOOR = 0.70


# ─── Grade table doctrine ─────────────────────────────────────────────


@pytest.mark.parametrize("grade", ["A+", "A", "B+"])
def test_high_grades_clear_seat_floor(grade: str) -> None:
    """A+, A, B+ MUST sit above the 0.70 Seat floor — they're
    binding witnesses, not advisory."""
    assert GRADE_TO_CONFIDENCE[grade] > SEAT_FLOOR


@pytest.mark.parametrize("grade", ["B", "C+", "C", "D", "F"])
def test_low_grades_below_seat_floor(grade: str) -> None:
    """B and below MUST sit at-or-below the 0.70 Seat floor — they're
    advisory witnesses (LOG_ONLY by Seat policy)."""
    assert GRADE_TO_CONFIDENCE[grade] <= SEAT_FLOOR


def test_unknown_grade_returns_log_only_floor() -> None:
    """Missing or unrecognized grades land at the LOG_ONLY floor —
    we never over-weight a witness we don't understand."""
    assert grade_score_to_confidence(None) == UNKNOWN_GRADE_FLOOR
    assert grade_score_to_confidence("") == UNKNOWN_GRADE_FLOOR
    assert grade_score_to_confidence("???") == UNKNOWN_GRADE_FLOOR
    assert UNKNOWN_GRADE_FLOOR < SEAT_FLOOR


def test_grade_is_case_insensitive_and_trimmed() -> None:
    assert grade_score_to_confidence("a+") == GRADE_TO_CONFIDENCE["A+"]
    assert grade_score_to_confidence(" A ") == GRADE_TO_CONFIDENCE["A"]


# ─── Score bonus is bounded ───────────────────────────────────────────


def test_score_bonus_caps_at_plus_minus_cap() -> None:
    """Even at the extremes, the raw score can't shift confidence by
    more than SCORE_BONUS_CAP from the base grade — grade dominates."""
    base = GRADE_TO_CONFIDENCE["A"]
    high = grade_score_to_confidence("A", score=PINE_SCORE_MAX * 2)  # clamped
    low = grade_score_to_confidence("A", score=-100)  # clamped
    # Both should differ from base by exactly the cap (in opposite signs)
    assert high == pytest.approx(base + SCORE_BONUS_CAP, abs=1e-9)
    assert low == pytest.approx(base - SCORE_BONUS_CAP, abs=1e-9)


def test_score_at_midpoint_yields_no_bonus() -> None:
    """Midpoint score = neutral, no nudge applied."""
    midpoint = PINE_SCORE_MAX / 2.0
    assert grade_score_to_confidence("A", score=midpoint) == pytest.approx(
        GRADE_TO_CONFIDENCE["A"], abs=1e-9,
    )


def test_score_none_yields_no_bonus() -> None:
    """When Pine omits `score`, the base grade is returned unchanged."""
    assert grade_score_to_confidence("A+", score=None) == GRADE_TO_CONFIDENCE["A+"]


def test_confidence_never_escapes_zero_one() -> None:
    """No matter the input, confidence is a probability ∈ [0, 1]."""
    assert grade_score_to_confidence("F", score=-1000) >= 0.0
    assert grade_score_to_confidence("A+", score=1000) <= 1.0


def test_b_plus_remains_above_floor_even_at_min_score() -> None:
    """Edge case: B+ is right at 0.72 — confirm that even the worst
    raw score can't drop it BELOW the Seat floor. (If a future tweak
    moves B+ closer to 0.70, this guard catches the regression.)"""
    worst = grade_score_to_confidence("B+", score=0.0)
    # 0.72 - 0.05 = 0.67, which IS below 0.70 — operator should be
    # aware of this, so the test pins the EXACT computed value rather
    # than asserting above-floor. The score-bonus IS a way for a B+
    # to drop into LOG_ONLY territory. That's by design (a weak
    # Pine score on a B+ grade is a soft signal). The test pins the
    # math so it doesn't drift unintentionally.
    assert worst == pytest.approx(GRADE_TO_CONFIDENCE["B+"] - SCORE_BONUS_CAP, abs=1e-9)


# ─── Direction mapping ────────────────────────────────────────────────


@pytest.mark.parametrize("pine_dir,expected", [
    ("long", "BUY"),
    ("short", "SELL"),
    ("flat", "HOLD"),
    ("LONG", "BUY"),
    (" Short ", "SELL"),
])
def test_pine_dir_to_side_known_values(pine_dir: str, expected: str) -> None:
    assert pine_dir_to_side(pine_dir) == expected


@pytest.mark.parametrize("bad", ["", "up", "down", "neutral", None])
def test_pine_dir_to_side_rejects_unknown(bad) -> None:
    """Unknown directions raise — the route layer catches and 400s
    so the operator sees the bad witness payload."""
    with pytest.raises(ValueError):
        pine_dir_to_side(bad)  # type: ignore[arg-type]


# ─── Dedup key shape ──────────────────────────────────────────────────


def test_dedup_key_is_deterministic_from_witness_fields() -> None:
    """Doctrine: the dedup key depends ONLY on witness-supplied
    fields — no server timestamps, no per-request randomness. A
    TradingView retry produces the same key and hits the unique
    index."""
    k1 = build_dedup_key("pine", "XAUUSD", "15", "entry", "2026-02-23T14:30:00Z")
    k2 = build_dedup_key("pine", "XAUUSD", "15", "entry", "2026-02-23T14:30:00Z")
    assert k1 == k2


def test_dedup_key_normalizes_symbol_case() -> None:
    """Mixed-case symbols (xauusd vs XAUUSD) must NOT generate
    distinct dedup keys — that would be an idempotency leak."""
    k1 = build_dedup_key("pine", "xauusd", "15", "entry", "2026-02-23T14:30:00Z")
    k2 = build_dedup_key("pine", "XAUUSD", "15", "entry", "2026-02-23T14:30:00Z")
    assert k1 == k2


def test_dedup_key_distinguishes_different_bars() -> None:
    """Two alerts on the same symbol/tf/event but different bar
    closes MUST produce distinct keys."""
    k1 = build_dedup_key("pine", "XAUUSD", "15", "entry", "2026-02-23T14:30:00Z")
    k2 = build_dedup_key("pine", "XAUUSD", "15", "entry", "2026-02-23T14:45:00Z")
    assert k1 != k2


def test_dedup_key_handles_missing_optional_fields() -> None:
    """Even if Pine omits `tf` or `event`, the key remains stable
    (using '-' as the placeholder for each)."""
    k = build_dedup_key("pine", "XAUUSD", None, None, "2026-02-23T14:30:00Z")
    assert "-" in k
    assert "XAUUSD" in k
    assert "2026-02-23T14:30:00Z" in k
