"""Tests for Pine self-reported confidence + dir→side + dedup key.

Doctrine pin: this module's `pine_self_reported_confidence` is
ADVISORY ONLY. The Seat does not act on it. The Governor does not
act on it. These tests therefore guard the MATH (monotonic,
bounded, deterministic) — NOT any "Seat floor" or "binding
threshold" doctrine, because no such doctrine applies in v1
(witnesses are default-hostile; all influence is zero until
Verifier promotes the source).
"""
from __future__ import annotations

import pytest

from shared.external_signals import (
    ExternalSignal,
    ExternalSourceCredibility,
    GRADE_TO_CONFIDENCE,
    SCORE_BONUS_CAP,
    UNKNOWN_GRADE_FLOOR,
    build_dedup_key,
    pine_dir_to_side,
    pine_self_reported_confidence,
)
from shared.external_signals.scoring import PINE_SCORE_MAX


# ─── Grade table is monotonic and bounded ─────────────────────────────


def test_grade_table_is_monotonic() -> None:
    """A+ > A > B+ > B > C+ > C > D > F. If the table ever drifts
    out of order, downstream comparisons (which the Verifier WILL
    use to correlate self-grade vs outcome) break."""
    order = ["A+", "A", "B+", "B", "C+", "C", "D", "F"]
    values = [GRADE_TO_CONFIDENCE[g] for g in order]
    for a, b in zip(values, values[1:]):
        assert a > b, f"grade table out of order: {order}"


def test_unknown_grade_returns_floor() -> None:
    assert pine_self_reported_confidence(None) == UNKNOWN_GRADE_FLOOR
    assert pine_self_reported_confidence("") == UNKNOWN_GRADE_FLOOR
    assert pine_self_reported_confidence("???") == UNKNOWN_GRADE_FLOOR


def test_grade_is_case_insensitive_and_trimmed() -> None:
    assert pine_self_reported_confidence("a+") == GRADE_TO_CONFIDENCE["A+"]
    assert pine_self_reported_confidence(" A ") == GRADE_TO_CONFIDENCE["A"]


# ─── Score bonus is bounded ───────────────────────────────────────────


def test_score_bonus_caps_at_plus_minus_cap() -> None:
    """Even at the extremes, the raw score can't shift confidence by
    more than SCORE_BONUS_CAP from the base grade — the grade
    dominates the rendered diagnostic value."""
    base = GRADE_TO_CONFIDENCE["A"]
    high = pine_self_reported_confidence("A", score=PINE_SCORE_MAX * 2)  # clamped
    low = pine_self_reported_confidence("A", score=-100)  # clamped
    assert high == pytest.approx(base + SCORE_BONUS_CAP, abs=1e-9)
    assert low == pytest.approx(base - SCORE_BONUS_CAP, abs=1e-9)


def test_score_at_midpoint_yields_no_bonus() -> None:
    midpoint = PINE_SCORE_MAX / 2.0
    assert pine_self_reported_confidence("A", score=midpoint) == pytest.approx(
        GRADE_TO_CONFIDENCE["A"], abs=1e-9,
    )


def test_score_none_yields_no_bonus() -> None:
    assert pine_self_reported_confidence("A+", score=None) == GRADE_TO_CONFIDENCE["A+"]


def test_confidence_never_escapes_zero_one() -> None:
    """No matter the input, the rendered float is a probability ∈ [0, 1]."""
    assert pine_self_reported_confidence("F", score=-1000) >= 0.0
    assert pine_self_reported_confidence("A+", score=1000) <= 1.0


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
    with pytest.raises(ValueError):
        pine_dir_to_side(bad)  # type: ignore[arg-type]


# ─── Dedup key shape ──────────────────────────────────────────────────


def test_dedup_key_is_deterministic_from_witness_fields() -> None:
    """Doctrine: the dedup key depends ONLY on witness-supplied
    fields. A TradingView retry produces the same key and hits the
    unique index."""
    k1 = build_dedup_key("pine", "XAUUSD", "15", "entry", "2026-02-23T14:30:00Z")
    k2 = build_dedup_key("pine", "XAUUSD", "15", "entry", "2026-02-23T14:30:00Z")
    assert k1 == k2


def test_dedup_key_normalizes_symbol_case() -> None:
    k1 = build_dedup_key("pine", "xauusd", "15", "entry", "2026-02-23T14:30:00Z")
    k2 = build_dedup_key("pine", "XAUUSD", "15", "entry", "2026-02-23T14:30:00Z")
    assert k1 == k2


def test_dedup_key_distinguishes_different_bars() -> None:
    k1 = build_dedup_key("pine", "XAUUSD", "15", "entry", "2026-02-23T14:30:00Z")
    k2 = build_dedup_key("pine", "XAUUSD", "15", "entry", "2026-02-23T14:45:00Z")
    assert k1 != k2


def test_dedup_key_handles_missing_optional_fields() -> None:
    k = build_dedup_key("pine", "XAUUSD", None, None, "2026-02-23T14:30:00Z")
    assert "-" in k
    assert "XAUUSD" in k


# ─── Default-hostile doctrine on the ExternalSignal model ─────────────


def test_external_signal_defaults_hostile() -> None:
    """Every witness lands UNTRUSTED with influence_allowed=False.
    This guard is the doctrine pin — if anyone ever changes the
    default to TRUSTED or True, this test catches it before code
    review."""
    s = ExternalSignal(
        source="pine",
        symbol="XAUUSD",
        side="BUY",
        self_reported_confidence=0.99,  # even max confidence — still hostile
        raw={"v": 2},
        bar_close_ts="2026-02-23T14:30:00Z",
        dedup_key="pine:XAUUSD:15:entry:2026-02-23T14:30:00Z",
    )
    assert s.verifier_status == "UNTRUSTED"
    assert s.influence_allowed is False
    assert s.processed_by_seat is False
    assert s.applied_modifier is None


def test_external_source_credibility_defaults_hostile() -> None:
    """Verifier case-file rows also land default-hostile. A first-
    sight witness MUST start at UNTRUSTED with zero samples."""
    c = ExternalSourceCredibility(source="pine")
    assert c.status == "UNTRUSTED"
    assert c.samples == 0
    assert c.wins == 0
    assert c.losses == 0
    assert c.verified_alpha == 0.0
    assert c.last_promoted_at is None
    assert c.last_demoted_at is None
