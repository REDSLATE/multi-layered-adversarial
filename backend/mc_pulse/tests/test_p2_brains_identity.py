"""P2 pulse brains — identity + behavior parity tests.

Doctrine 2026-07-12: all 4 pulse brains wrap the same core
(`NeutralAdversarialBrain`) and differ ONLY in personality
multiplier + branding. This test file guards that contract:

    1. Each brain has the correct identity constants.
    2. Each brain's personality multiplier matches the mapping in
       `external/brains/personality.py::BRAIN_PERSONALITIES`.
    3. Each brain produces distinct rationale strings (RATIONALE_TAG
       makes the voice visible in logs) — so operator can tell whose
       voice is on the tape.
    4. The Brain protocol properties (`id`, `lanes`, `cadence_seconds`,
       `evaluation_timeout_seconds`) resolve correctly through the
       property indirection.
    5. Independent instances: two GtoBrain instances have independent
       cool-down state (i.e., subclasses don't accidentally share
       class-level mutable state).
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from mc_brains._legacy.personality import BRAIN_PERSONALITIES
from mc_brains.barracuda import BarracudaBrain
from mc_brains.camino import CaminoBrain
from mc_brains.gto import GtoBrain
from mc_brains.hellcat import HellcatBrain
from mc_pulse.protocols import Brain


ALL_BRAINS = (CaminoBrain, GtoBrain, BarracudaBrain, HellcatBrain)


def test_all_brains_conform_to_protocol():
    for cls in ALL_BRAINS:
        assert isinstance(cls(), Brain), (
            f"{cls.__name__} does not satisfy mc_pulse.protocols.Brain"
        )


def test_pulse_ids_are_unique_and_lowercase():
    ids = [cls.PULSE_ID for cls in ALL_BRAINS]
    assert len(set(ids)) == 4, f"duplicate pulse ids: {ids}"
    for pid in ids:
        assert pid == pid.lower(), f"pulse id {pid!r} must be lowercase"


def test_core_brain_ids_match_personality_module():
    """Every pulse brain's CORE_BRAIN_ID must exist in the
    personality module, and the DISPLAY_NAME must match."""
    for cls in ALL_BRAINS:
        assert cls.CORE_BRAIN_ID in BRAIN_PERSONALITIES, (
            f"{cls.__name__} CORE_BRAIN_ID={cls.CORE_BRAIN_ID!r} "
            f"not in personality module"
        )
        expected_name = BRAIN_PERSONALITIES[cls.CORE_BRAIN_ID]["display_name"]
        assert cls.DISPLAY_NAME == expected_name


def test_personality_multipliers_match_expected():
    """The 4 brains have the operator-locked multipliers."""
    expected = {
        "camino": 1.00,      # alpha, balanced
        "gto": 0.85,         # redeye, disciplined
        "barracuda": 1.15,   # camaro, opportunistic
        "hellcat": 1.30,     # chevelle, aggressive
    }
    for cls in ALL_BRAINS:
        mult = BRAIN_PERSONALITIES[cls.CORE_BRAIN_ID]["confidence_mult"]
        assert mult == expected[cls.PULSE_ID], (
            f"{cls.PULSE_ID} multiplier drift: got {mult}, "
            f"expected {expected[cls.PULSE_ID]}"
        )


def test_rationale_tags_are_distinct():
    tags = {cls.RATIONALE_TAG for cls in ALL_BRAINS}
    assert len(tags) == 4, f"rationale tags collided: {tags}"


def test_brain_protocol_properties_resolve():
    for cls in ALL_BRAINS:
        b = cls()
        assert b.id == cls.PULSE_ID
        assert b.lanes == cls.LANES
        assert isinstance(b.cadence_seconds, int)
        assert isinstance(b.evaluation_timeout_seconds, float)


def test_instances_have_independent_cooldown_state():
    """Two GtoBrain instances must NOT share `_last_eval_at`.
    Guards against a subtle class-level mutable-default regression."""
    a = GtoBrain()
    b = GtoBrain()
    a._last_eval_at["equity:AAPL"] = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert "equity:AAPL" not in b._last_eval_at


def test_missing_identity_constants_raises_on_construct():
    """Base class MUST refuse instantiation without identity."""
    from mc_brains._pulse_base import NeutralAdversarialPulseBrain

    class Broken(NeutralAdversarialPulseBrain):
        pass  # missing PULSE_ID / CORE_BRAIN_ID / DISPLAY_NAME

    with pytest.raises(TypeError, match="PULSE_ID"):
        Broken()
