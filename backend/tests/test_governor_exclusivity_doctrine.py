"""Tripwires — governor-exclusivity doctrine (2026-05-26).

Doctrine pin:
    All seats are open to all four brains EXCEPT `governor` and its
    crypto twin `crypto_governor`, which are EXCLUSIVE to Chevelle
    and RedEye. The endpoint and the assignment validator must
    enforce this — operator cannot loosen it via the matrix UI.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from shared.roster import (
    DEFAULT_ELIGIBILITY,
    _GOVERNOR_EXCLUSIVE_BRAINS,
    _GOVERNOR_EXCLUSIVE_SEATS,
    _ensure_assignment_eligible,
)


pytestmark = pytest.mark.asyncio


# ─────────── A — DEFAULT_ELIGIBILITY shape ───────────


def test_governor_seats_locked_for_alpha():
    """Default matrix MUST refuse alpha for governor and crypto_governor."""
    assert DEFAULT_ELIGIBILITY["alpha"]["governor"] is False
    assert DEFAULT_ELIGIBILITY["alpha"]["crypto_governor"] is False


def test_governor_seats_locked_for_camaro():
    assert DEFAULT_ELIGIBILITY["camaro"]["governor"] is False
    assert DEFAULT_ELIGIBILITY["camaro"]["crypto_governor"] is False


def test_governor_seats_open_for_chevelle():
    assert DEFAULT_ELIGIBILITY["chevelle"]["governor"] is True
    assert DEFAULT_ELIGIBILITY["chevelle"]["crypto_governor"] is True


def test_governor_seats_open_for_redeye():
    assert DEFAULT_ELIGIBILITY["redeye"]["governor"] is True
    assert DEFAULT_ELIGIBILITY["redeye"]["crypto_governor"] is True


def test_non_governor_seats_open_for_all_brains():
    """Every brain MUST default-True on every non-governor seat. This is
    the "all seats available to all brains" half of the doctrine."""
    non_gov = [
        s for s in DEFAULT_ELIGIBILITY["alpha"]
        if s not in _GOVERNOR_EXCLUSIVE_SEATS
    ]
    for brain in ("alpha", "camaro", "chevelle", "redeye"):
        for seat in non_gov:
            assert DEFAULT_ELIGIBILITY[brain][seat] is True, (
                f"{brain}.{seat} should default True under new doctrine"
            )


def test_governor_exclusive_seats_constant():
    """The seat list MUST be exactly governor + crypto_governor.
    Don't silently expand."""
    assert set(_GOVERNOR_EXCLUSIVE_SEATS) == {"governor", "crypto_governor"}


def test_governor_exclusive_brains_constant():
    """Eligibility set MUST be exactly Chevelle + RedEye."""
    assert set(_GOVERNOR_EXCLUSIVE_BRAINS) == {"chevelle", "redeye"}


# ─────────── B — Assignment validator ───────────


async def test_assign_alpha_governor_rejected():
    """Putting alpha into governor MUST raise 400 with a clear
    doctrine message, BEFORE any matrix lookup."""
    with pytest.raises(HTTPException) as exc:
        await _ensure_assignment_eligible("governor", "alpha")
    assert exc.value.status_code == 400
    assert "exclusive" in exc.value.detail.lower()


async def test_assign_camaro_crypto_governor_rejected():
    with pytest.raises(HTTPException) as exc:
        await _ensure_assignment_eligible("crypto_governor", "camaro")
    assert exc.value.status_code == 400


async def test_assign_chevelle_governor_accepted():
    """Putting Chevelle into governor MUST proceed past the doctrine
    guard. (May still raise on a stricter matrix override, but the
    governor-exclusivity check itself must pass.)"""
    # No raise = pass.
    await _ensure_assignment_eligible("governor", "chevelle")


async def test_assign_redeye_governor_accepted():
    await _ensure_assignment_eligible("governor", "redeye")


async def test_vacate_governor_always_allowed():
    """Vacating (brain=None) MUST always succeed regardless of seat."""
    await _ensure_assignment_eligible("governor", None)
    await _ensure_assignment_eligible("crypto_governor", None)


async def test_assign_alpha_executor_passes_doctrine_guard():
    """Doctrine guard only fires on governor seats. Alpha→executor
    must NOT be blocked by it."""
    # If this raises, the guard is over-broad.
    try:
        await _ensure_assignment_eligible("executor", "alpha")
    except HTTPException as e:
        # Acceptable only if the message is about the matrix, NOT
        # about governor exclusivity. (Won't happen with default
        # matrix but guard against future regressions.)
        assert "exclusive" not in (e.detail or "").lower()
