"""PARADOX hierarchy — namespace lock tests.

Doctrine (2026-05-20): Each role is anchored to exactly one runtime.
There is no auditor seat. The kernel sits above the brains.

These tests pin the hierarchy. Drift = doctrine violation.
"""
from __future__ import annotations

import pytest

from namespaces import (
    LIVE_RUNTIMES,
    OPPONENT_MODE_LIVE,
    OPPONENT_MODE_OFFLINE,
    OPPONENT_MODE_SHADOW,
    PARADOX_KERNEL,
    PARADOX_RECORDS,
    ROLE_ANCHORS,
    RUNTIME_ROLE,
    RUNTIMES,
)
from shared.seat_policy import SEAT_ALIASES, normalize_seat


# ───── ROLE_ANCHORS table — locked ────────────────────────────────────


@pytest.mark.tripwire
def test_paradox_role_anchors_locked():
    """The 5 role→runtime anchors are doctrine. Adding, removing,
    or re-pointing any of them requires explicit operator review."""
    assert ROLE_ANCHORS == {
        "strategist": "alpha",
        "executor":   "camaro",
        "governor":   "chevelle",
        "opponent":   "redeye",
        "memory":     "shelly",
    }


@pytest.mark.tripwire
def test_no_auditor_seat():
    """Auditor is NOT a seat. It is an emergent function — the
    paradox_record artifact produced by (executor, opponent)."""
    assert "auditor" not in ROLE_ANCHORS
    assert "auditor" not in RUNTIME_ROLE.values()


@pytest.mark.tripwire
def test_runtime_role_reverse_lookup_consistent():
    for role, runtime in ROLE_ANCHORS.items():
        assert RUNTIME_ROLE[runtime] == role


@pytest.mark.tripwire
def test_kernel_name_is_paradox():
    """The kernel above the brains is PARADOX, not ENIGMA."""
    assert PARADOX_KERNEL == "PARADOX"


@pytest.mark.tripwire
def test_paradox_records_collection_name_stable():
    assert PARADOX_RECORDS == "paradox_records"


# ───── Live runtimes vs reserved namespace ────────────────────────────


@pytest.mark.tripwire
def test_shelly_is_reserved_not_live():
    """Shelly has a reserved role slot but is not a running sidecar
    yet. The system must not expect a check-in from Shelly until it
    actually ships."""
    assert "shelly" in ROLE_ANCHORS.values()
    assert "shelly" not in LIVE_RUNTIMES
    assert "shelly" not in RUNTIMES  # RUNTIMES = live sidecars


@pytest.mark.tripwire
def test_live_runtimes_set():
    assert set(LIVE_RUNTIMES) == {"alpha", "camaro", "chevelle", "redeye"}


# ───── Opponent mode constants ────────────────────────────────────────


@pytest.mark.tripwire
def test_opponent_mode_constants_locked():
    """Exactly 3 valid opponent modes; each must keep its string
    value because paradox_records and audit code grep for them."""
    assert OPPONENT_MODE_LIVE == "live"
    assert OPPONENT_MODE_SHADOW == "shadow_observation"
    assert OPPONENT_MODE_OFFLINE == "offline"


# ───── Seat aliases — auditor correction ──────────────────────────────


@pytest.mark.tripwire
def test_advisor_aliases_to_opponent_not_auditor():
    """The 2026-05-20 correction: advisor → opponent (not auditor).
    Auditor was never a destination — it is an emergent function."""
    assert SEAT_ALIASES["advisor"] == "opponent"
    assert SEAT_ALIASES["crypto_advisor"] == "crypto_opponent"


@pytest.mark.tripwire
def test_auditor_aliases_to_opponent_for_legacy_reads():
    """Old code/data still using `seat=auditor` should transparently
    resolve to the opponent seat. Auditor never grants a seat."""
    assert SEAT_ALIASES["auditor"] == "opponent"
    assert SEAT_ALIASES["crypto_auditor"] == "crypto_opponent"


@pytest.mark.tripwire
def test_decider_alias_unchanged():
    """decider → executor is the original alias and stays."""
    assert SEAT_ALIASES["decider"] == "executor"
    assert SEAT_ALIASES["crypto_decider"] == "crypto"


def test_normalize_seat_advisor_yields_opponent():
    assert normalize_seat("advisor") == "opponent"
    assert normalize_seat("auditor") == "opponent"
    assert normalize_seat("decider") == "executor"


def test_normalize_seat_canonical_passthrough():
    for canon in ("executor", "governor", "opponent", "strategist", "memory"):
        assert normalize_seat(canon) == canon
