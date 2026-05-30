"""Position-model quorum doctrine tests (2026-05-30).

Doctrine: a required seat is "engaged" iff the brain currently
HOLDING that seat has authored a stance on the position. After
rotation, the previous holder's stance no longer satisfies the
new holder's quorum — authority moved with the seat.

These are pure-function tests against `_compute_quorum`. Live-API
quorum integration coverage lives in `test_quorum_and_provenance.py`.
"""
from __future__ import annotations

import asyncio

import pytest

from shared.positions import _compute_quorum


def _stance(brain: str, posted_as: str, stance: str = "long") -> dict:
    return {
        "brain": brain,
        "posted_as": posted_as,
        "stance": stance,
        "confidence": 0.7,
    }


def test_seat_engaged_when_current_holder_has_stance():
    """The straightforward case: Alpha holds executor, Alpha stanced
    → executor seat is engaged."""
    stances_by_brain = {"alpha": _stance("alpha", "executor")}
    stances_by_seat = {"executor": _stance("alpha", "executor")}
    roster = {"executor": "alpha", "governor": "chevelle"}
    q = asyncio.run(_compute_quorum(stances_by_brain, stances_by_seat, roster))
    assert "executor" in q["seats_engaged"]
    assert "executor" not in q["seats_missing"]


def test_seat_missing_when_current_holder_silent_even_if_predecessor_spoke():
    """The doctrine-critical case: Camaro previously held executor and
    stanced. Operator rotates to Alpha. Alpha has not yet spoken.

    OLD model: stances_by_seat['executor'] = Camaro's old stance →
    "engaged" → false sense of quorum.

    NEW (position) model: current executor = Alpha; Alpha not in
    stances_by_brain → executor seat is MISSING. Alpha must re-speak.
    """
    stances_by_brain = {"camaro": _stance("camaro", "executor")}
    stances_by_seat = {"executor": _stance("camaro", "executor")}  # stale residue
    roster = {"executor": "alpha", "governor": "chevelle"}
    q = asyncio.run(_compute_quorum(stances_by_brain, stances_by_seat, roster))
    assert "executor" not in q["seats_engaged"], (
        "OLD doctrine bug: Camaro's residue still counted as executor engagement "
        "after seat rotation to Alpha"
    )
    assert "executor" in q["seats_missing"]


def test_vacant_required_seat_marked_vacant_not_just_missing():
    """A seat with no current holder is both `vacant_required` and
    `missing`. UI shows both — vacant is louder than just silent."""
    stances_by_brain = {}
    stances_by_seat = {}
    roster = {"executor": "alpha", "auditor": None}
    q = asyncio.run(_compute_quorum(stances_by_brain, stances_by_seat, roster))
    if "auditor" in q["seats_required"]:
        assert "auditor" in q["vacant_required_seats"]
        assert "auditor" in q["seats_missing"]


def test_one_brain_holding_two_seats_engages_both_with_single_stance():
    """Edge case: if one brain holds multiple required seats, a single
    stance from that brain engages every seat they hold. Position-model
    semantic — the brain speaks with the authority of whatever seats
    they're sitting in."""
    stances_by_brain = {"alpha": _stance("alpha", "executor")}
    stances_by_seat = {"executor": _stance("alpha", "executor")}
    roster = {"executor": "alpha", "strategist": "alpha", "governor": "chevelle"}
    q = asyncio.run(_compute_quorum(stances_by_brain, stances_by_seat, roster))
    if "executor" in q["seats_required"]:
        assert "executor" in q["seats_engaged"]
    if "strategist" in q["seats_required"]:
        assert "strategist" in q["seats_engaged"]


def test_degraded_flag_true_iff_any_required_seat_missing_or_vacant():
    stances_by_brain = {"alpha": _stance("alpha", "executor")}
    stances_by_seat = {"executor": _stance("alpha", "executor")}
    roster = {"executor": "alpha"}  # all other required seats vacant
    q = asyncio.run(_compute_quorum(stances_by_brain, stances_by_seat, roster))
    # If any required seat is vacant or unstanced, degraded must be True.
    assert q["degraded"] is True
    # Adversarial / governance blindness reflect specific missing seats.
    if "opponent" in q["seats_required"]:
        assert q["adversarial_blindness"] is True
    if "governor" in q["seats_required"]:
        assert q["governance_blindness"] is True


def test_governance_blindness_clears_when_current_governor_speaks():
    """Chevelle holds governor and has stanced → governance_blindness
    must be False."""
    stances_by_brain = {
        "alpha": _stance("alpha", "executor"),
        "chevelle": _stance("chevelle", "governor"),
    }
    stances_by_seat = {
        "executor": _stance("alpha", "executor"),
        "governor": _stance("chevelle", "governor"),
    }
    roster = {"executor": "alpha", "governor": "chevelle"}
    q = asyncio.run(_compute_quorum(stances_by_brain, stances_by_seat, roster))
    assert q["governance_blindness"] is False
    assert "governor" in q["seats_engaged"]


def test_governance_blindness_persists_after_rotation_if_new_governor_silent():
    """Doctrine teeth: Chevelle stanced under governor. Operator rotates
    governor → Alpha. Alpha hasn't spoken. governance_blindness must
    flip BACK to True — Alpha's silence in the governor chair IS
    governance blindness, regardless of Chevelle's historical stance."""
    stances_by_brain = {"chevelle": _stance("chevelle", "governor")}
    stances_by_seat = {"governor": _stance("chevelle", "governor")}  # stale
    roster = {"executor": "camaro", "governor": "alpha"}  # rotated
    q = asyncio.run(_compute_quorum(stances_by_brain, stances_by_seat, roster))
    assert q["governance_blindness"] is True, (
        "OLD doctrine bug: Chevelle's pre-rotation stance still hid "
        "Alpha's silence in the governor chair"
    )
    assert "governor" in q["seats_missing"]
