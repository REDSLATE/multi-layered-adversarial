"""Tests for `shared.governor_policy.apply_governor_policy` — the
reusable FATAL/SILENCE post-process for governance dicts.

The taxonomy itself is pinned in test_governance_verdict.py; this
suite pins the standalone export's input/output contract so any
caller (ingest path, discussion layer, future channels) gets
consistent FATAL/SILENCE decisions on the same inputs.
"""
from __future__ import annotations

import pytest

from shared.governor_policy import (
    SILENCE_OR_SOFT_REASONS,
    SILENCE_RISK_FLOOR,
    SILENCE_RISK_HALVING,
    apply_governor_policy,
)

pytestmark = pytest.mark.tripwire


# ─────────────────────── non-block statuses pass through ─────────────


def test_allow_status_passes_through():
    executable, size_mult, gov = apply_governor_policy(
        {"status": "ALLOW", "reason": "NO_GOVERNOR_DISSENT"},
        executable=True, size_mult=1.0,
    )
    assert executable is True
    assert size_mult == 1.0
    assert gov["execution_effect"] == "ALLOW"
    assert gov["display_status"] == "ALLOW"


def test_warn_status_passes_through_with_display_status_set():
    executable, size_mult, gov = apply_governor_policy(
        {"status": "WARN", "reason": "BELOW_RISK_BUDGET"},
        executable=True, size_mult=0.75,
    )
    assert executable is True
    assert size_mult == 0.75
    assert gov["display_status"] == "WARN"


def test_empty_status_defaults_to_allow():
    executable, size_mult, gov = apply_governor_policy(
        {},
        executable=True, size_mult=1.0,
    )
    assert executable is True
    assert size_mult == 1.0
    assert gov["display_status"] == "ALLOW"


# ─────────────────────── FATAL kills ─────────────────────────────────


def test_hard_veto_kills_execution():
    executable, size_mult, gov = apply_governor_policy(
        {"status": "BLOCK", "reason": "GOVERNOR_HARD_VETO"},
        executable=True, size_mult=1.0,
    )
    assert executable is False
    assert size_mult == 0.0
    assert gov["execution_effect"] == "HARD_BLOCK"
    assert gov["display_status"] == "BLOCK"


def test_all_fatal_reasons_kill_execution():
    fatal = ["GOVERNOR_HARD_VETO", "KILL_SWITCH_ACTIVE", "BROKER_UNAVAILABLE",
             "AUTH_MISSING", "SYMBOL_UNRESOLVED", "MAX_EXPOSURE_EXCEEDED",
             "PDT_BLOCK", "DUPLICATE_POSITION", "GOVERNOR_SEAT_VACANT"]
    for r in fatal:
        executable, size_mult, _ = apply_governor_policy(
            {"status": "BLOCK", "reason": r},
            executable=True, size_mult=1.0,
        )
        assert executable is False, f"{r} must be FATAL"
        assert size_mult == 0.0


# ─────────────────────── SILENCE / SOFT → RISK_DOWN ──────────────────


def test_governor_offline_is_silence_risk_down():
    executable, size_mult, gov = apply_governor_policy(
        {"status": "BLOCK", "reason": "GOVERNOR_OFFLINE"},
        executable=True, size_mult=1.0,
    )
    assert executable is True
    assert size_mult == pytest.approx(SILENCE_RISK_HALVING)
    assert gov["execution_effect"] == "RISK_DOWN_ONLY"
    assert gov["display_status"] == "RISK_DOWN"


def test_all_silence_or_soft_reasons_become_risk_down():
    for r in SILENCE_OR_SOFT_REASONS:
        executable, size_mult, gov = apply_governor_policy(
            {"status": "BLOCK", "reason": r},
            executable=True, size_mult=1.0,
        )
        assert executable is True, f"{r} must NOT be FATAL"
        assert gov["execution_effect"] == "RISK_DOWN_ONLY"
        assert gov["display_status"] == "RISK_DOWN"
        assert size_mult > 0


def test_risk_floor_never_zeroes_silence():
    """A 0.0 size_mult input should still emerge at SILENCE_RISK_FLOOR
    (operator-spec'd 10% floor) instead of zeroing the trade."""
    executable, size_mult, gov = apply_governor_policy(
        {"status": "BLOCK", "reason": "GOVERNOR_OFFLINE"},
        executable=True, size_mult=0.0,
    )
    assert executable is True
    assert size_mult == pytest.approx(SILENCE_RISK_FLOOR)


def test_unknown_block_reason_treated_as_conservative_risk_down():
    """Unknown BLOCK reason → soft (NOT kill). Operator can promote
    to FATAL_GOVERNOR_REASONS if it should kill."""
    executable, size_mult, gov = apply_governor_policy(
        {"status": "BLOCK", "reason": "WEIRD_NEW_REASON_NOT_IN_TAXONOMY"},
        executable=True, size_mult=1.0,
    )
    assert executable is True
    assert gov["execution_effect"] == "RISK_DOWN_ONLY"
    assert size_mult == pytest.approx(SILENCE_RISK_HALVING)


# ─────────────────────── input not mutated ────────────────────────────


def test_input_governance_not_mutated():
    inp = {"status": "BLOCK", "reason": "GOVERNOR_OFFLINE"}
    _, _, out = apply_governor_policy(inp, executable=True, size_mult=1.0)
    assert "execution_effect" not in inp  # original untouched
    assert out["execution_effect"] == "RISK_DOWN_ONLY"  # copy mutated


def test_case_insensitive_inputs():
    executable, _, gov = apply_governor_policy(
        {"status": "block", "reason": "governor_hard_veto"},
        executable=True, size_mult=1.0,
    )
    assert executable is False
    assert gov["display_status"] == "BLOCK"


def test_already_blocked_intent_stays_blocked_under_silence():
    """If the caller hands us executable=False (some upstream gate
    already blocked), silence shouldn't magically un-block — it can
    only RISK_DOWN an otherwise-executable intent."""
    executable, _, _ = apply_governor_policy(
        {"status": "BLOCK", "reason": "GOVERNOR_OFFLINE"},
        executable=False, size_mult=0.5,
    )
    # Silence path returns the incoming executable bool — so False stays False.
    assert executable is False
