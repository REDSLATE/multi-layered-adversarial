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
def test_no_auditor_paradox_anchor():
    """Auditor is a ROSTER SEAT (operator-assigned, dynamic) but it is
    NOT a PARADOX role anchor. ROLE_ANCHORS pins one runtime per
    architectural role (alpha=strategist, camaro=executor, etc.); the
    auditor seat has no permanent runtime anchor — any eligible brain
    may rotate into it."""
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


# ───── Seat aliases — opponent merged into auditor 2026-05-27 ─────────


@pytest.mark.tripwire
def test_advisor_aliases_to_auditor_post_merge():
    """2026-05-27 update: opponent was merged into auditor. The
    advisor alias was previously `advisor → opponent`; it's now
    `advisor → auditor` (same end state — the absorbed seat).
    Old roster docs storing `advisor` reads continue to resolve to a
    valid policy row."""
    assert SEAT_ALIASES["advisor"] == "auditor"
    assert SEAT_ALIASES["crypto_advisor"] == "crypto_auditor"


@pytest.mark.tripwire
def test_opponent_aliases_to_auditor():
    """2026-05-27: opponent → auditor (operator merge). Legacy code
    paths that still read `opponent` resolve to the auditor seat."""
    assert SEAT_ALIASES["opponent"] == "auditor"
    assert SEAT_ALIASES["crypto_opponent"] == "crypto_auditor"


@pytest.mark.tripwire
def test_auditor_is_real_seat_no_alias():
    """2026-05-24: Auditor was reinstated as a real roster seat. It no
    longer aliases to anything — `auditor` and `crypto_auditor` resolve
    to their own SEAT_POLICY rows. 2026-05-27: auditor also absorbed
    the opponent role; the seat itself remains canonical."""
    assert "auditor" not in SEAT_ALIASES
    assert "crypto_auditor" not in SEAT_ALIASES


@pytest.mark.tripwire
def test_decider_alias_unchanged():
    """decider → executor is the original legacy alias and stays.
    Note: the canonical roster slot was renamed `strategist` on
    2026-05-24, but `decider` still resolves to `executor` for old
    receipts that recorded the pre-rename name."""
    assert SEAT_ALIASES["decider"] == "executor"
    assert SEAT_ALIASES["crypto_decider"] == "crypto"


def test_normalize_seat_advisor_yields_auditor():
    """2026-05-27: advisor → auditor (was: advisor → opponent, before
    opponent was merged into auditor)."""
    assert normalize_seat("advisor") == "auditor"
    # auditor is the canonical merged seat — passes through unchanged.
    assert normalize_seat("auditor") == "auditor"
    assert normalize_seat("decider") == "executor"
    # opponent → auditor (the merge)
    assert normalize_seat("opponent") == "auditor"


def test_normalize_seat_canonical_passthrough():
    """Canonical seat names must pass through unchanged. Note that
    `opponent` is NO LONGER canonical (merged into auditor on
    2026-05-27) — it's now an alias, exercised by the test above."""
    for canon in ("executor", "governor", "strategist",
                  "auditor", "memory"):
        assert normalize_seat(canon) == canon
