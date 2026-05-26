"""Tripwires — Memory Modulator bound enforcement (2026-05-25).

Operator doctrine:
    Any `memory_modulator` payload on an incoming intent MUST have
    `value` ∈ [-0.25, +0.10]. Out-of-bound is a HARD REJECT (422).
    MC does NOT silently clamp — a brain shipping out-of-bound is
    buggy and must surface.

These tests cover IntentIn's validator only (the contract MC enforces
on every incoming brain receipt). They do not exercise the full
intent route — that's separately covered.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from shared.intents import IntentIn


_BASE = dict(
    stack="alpha",
    action="BUY",
    symbol="AAPL",
    lane="equity",
    confidence=0.5,
    rationale="modulator-bounds test",
)


def test_modulator_within_bounds_accepts():
    intent = IntentIn(
        **_BASE,
        memory_modulator={
            "value": 0.05,
            "matched_winners": 7,
            "matched_losers": 0,
            "reason": "matched_7_prior_winners",
        },
    )
    assert intent.memory_modulator["value"] == 0.05


def test_modulator_lower_bound_inclusive():
    intent = IntentIn(
        **_BASE,
        memory_modulator={"value": -0.25, "reason": "max_loss_dampen"},
    )
    assert intent.memory_modulator["value"] == -0.25


def test_modulator_upper_bound_inclusive():
    intent = IntentIn(
        **_BASE,
        memory_modulator={"value": 0.10, "reason": "max_win_upweight"},
    )
    assert intent.memory_modulator["value"] == 0.10


def test_modulator_below_min_rejected():
    with pytest.raises(ValidationError, match="out of doctrine bounds"):
        IntentIn(
            **_BASE,
            memory_modulator={"value": -0.30, "reason": "way_too_negative"},
        )


def test_modulator_above_max_rejected():
    with pytest.raises(ValidationError, match="out of doctrine bounds"):
        IntentIn(
            **_BASE,
            memory_modulator={"value": 0.40, "reason": "way_too_positive"},
        )


def test_modulator_legacy_alias_modulator_key_accepted():
    """Brains using the historical key `modulator` (not `value`) MUST
    still validate while we transition."""
    intent = IntentIn(
        **_BASE,
        memory_modulator={"modulator": -0.10, "reason": "legacy_alias"},
    )
    assert intent.memory_modulator.get("value") == -0.10


def test_modulator_missing_value_rejected():
    with pytest.raises(ValidationError, match="numeric `value`"):
        IntentIn(
            **_BASE,
            memory_modulator={"reason": "no value field provided"},
        )


def test_modulator_non_numeric_value_rejected():
    with pytest.raises(ValidationError, match="must be numeric"):
        IntentIn(
            **_BASE,
            memory_modulator={"value": "nope"},
        )


def test_modulator_optional_omitted():
    """Backward-compat: brains that don't ship a modulator MUST still
    have their intent accepted."""
    intent = IntentIn(**_BASE)
    assert intent.memory_modulator is None


def test_modulator_size_capped():
    """Receipts above 4KB serialized are rejected (anti-smuggling)."""
    big = {"value": 0.0, "junk": "x" * 5000}
    with pytest.raises(ValidationError, match="≤4 KB"):
        IntentIn(**_BASE, memory_modulator=big)


def test_modulator_non_dict_rejected():
    """Pydantic enforces dict shape before our validator — accept
    either its built-in `dict_type` error OR our explicit `must be an
    object` message."""
    with pytest.raises(ValidationError):
        IntentIn(**_BASE, memory_modulator="not a dict")
