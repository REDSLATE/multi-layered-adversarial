"""ModelOpinion / DaweState / RuntimeMode contracts."""
from __future__ import annotations

import pytest

from mc_arbiter.models import (
    DaweState,
    Direction,
    ModelOpinion,
    RuntimeMode,
    SeatDoc,
)


def _opinion(**overrides):
    base = dict(
        brain="camino",
        seat_key="equity:NVDA:2026-07-11T14:30:00Z",
        direction=Direction.LONG,
        edge=0.60,
        confidence=0.75,
        regime_fit=0.80,
        urgency=0.50,
        price_at_signal=100.0,
        ts="2026-07-11T14:31:22Z",
    )
    base.update(overrides)
    return ModelOpinion(**base)


# ── ModelOpinion.rank_score ──────────────────────────────────────

def test_rank_score_baseline():
    # 0.60 × 0.75 × 0.80 × (0.75 + 0.50 × 0.25) = 0.36 × 0.875 = 0.315
    op = _opinion()
    assert op.rank_score == pytest.approx(0.315, rel=1e-6)


def test_rank_score_zero_when_any_multiplier_zero():
    # Zero confidence → zero rank. The formula multiplies, so any
    # zero term wipes the score — this is intentional (Bruce Lee:
    # no fudge factor "just to keep the brain in the game").
    assert _opinion(confidence=0.0).rank_score == 0.0
    assert _opinion(edge=0.0).rank_score == 0.0
    # regime_fit is bounded [0.4, 1.0] but we still let 0.0 through
    # in the dataclass — the route boundary is where validation
    # lives. Rank still zeros out.
    assert _opinion(regime_fit=0.0).rank_score == 0.0


def test_rank_score_urgency_never_zeroes_the_score():
    # urgency=0.0 → multiplier = 0.75, not 0.0. Urgency dampens but
    # does not silence.
    op = _opinion(urgency=0.0)
    assert op.rank_score == pytest.approx(0.60 * 0.75 * 0.80 * 0.75, rel=1e-6)


def test_flat_direction_still_produces_rank_score():
    # FLAT is graded (see design freeze §3) — it must expose
    # rank_score for the grader even though it never wins a seat.
    op = _opinion(direction=Direction.FLAT)
    assert op.rank_score > 0.0


def test_opinion_is_frozen():
    op = _opinion()
    with pytest.raises(Exception):
        op.confidence = 0.99  # type: ignore[misc]


# ── Direction / RuntimeMode enums ─────────────────────────────────

def test_direction_enum_values():
    assert Direction.LONG.value == "LONG"
    assert Direction.SHORT.value == "SHORT"
    assert Direction.FLAT.value == "FLAT"
    # No BUY/SELL alias — matches doctrine.
    assert set(Direction.__members__) == {"LONG", "SHORT", "FLAT"}


def test_runtime_mode_has_only_two_values():
    # Design freeze §9: only DISARMED and LIVE. No PAPER, no SHADOW.
    # This test exists to fire if someone adds a third mode without
    # updating the design.
    assert set(RuntimeMode.__members__) == {"DISARMED", "LIVE"}


# ── DaweState round-trip ─────────────────────────────────────────

def test_dawe_state_default_is_neutral():
    s = DaweState(brain="camino", lane="equity")
    assert s.session_weight == 1.0
    assert s.recent_weight == 1.0
    assert s.prior_weight == 1.0
    assert s.grades_used_session == 0
    assert s.last_updated == ""


def test_dawe_state_mongo_roundtrip():
    s = DaweState(
        brain="hellcat",
        lane="crypto",
        session_weight=1.15,
        recent_weight=0.88,
        prior_weight=1.00,
        grades_used_session=42,
        grades_used_recent=210,
        last_updated="2026-07-11T14:35:00Z",
    )
    doc = s.to_mongo()
    assert doc == {
        "session": 1.15,
        "recent": 0.88,
        "prior": 1.00,
        "grades_used_session": 42,
        "grades_used_recent": 210,
        "last_updated": "2026-07-11T14:35:00Z",
    }
    restored = DaweState.from_mongo("hellcat", "crypto", doc)
    assert restored == s


def test_dawe_state_from_mongo_handles_missing_doc():
    # A never-graded (brain, lane) reads as neutral defaults.
    restored = DaweState.from_mongo("gto", "crypto", None)
    assert restored.brain == "gto"
    assert restored.lane == "crypto"
    assert restored.session_weight == 1.0
    assert restored.grades_used_session == 0


def test_dawe_state_from_mongo_handles_partial_doc():
    # Legacy or partial write — missing keys fall back to neutral,
    # never raise KeyError.
    restored = DaweState.from_mongo(
        "camino", "equity", {"session": 1.22},
    )
    assert restored.session_weight == 1.22
    assert restored.recent_weight == 1.0
    assert restored.grades_used_session == 0


# ── SeatDoc ──────────────────────────────────────────────────────

def test_seat_doc_defaults():
    s = SeatDoc(
        seat_key="equity:NVDA:2026-07-11T14:30:00Z",
        lane="equity",
        symbol="NVDA",
        bucket_iso="2026-07-11T14:30:00Z",
    )
    assert s.opinions == []
    assert s.winner is None
    assert s.runtime_mode == RuntimeMode.DISARMED.value
