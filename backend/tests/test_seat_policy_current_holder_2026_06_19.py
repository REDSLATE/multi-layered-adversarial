"""Regression test for the 2026-06-19 Prod "Camino sold equity" incident.

Symptom (Production):
    The operator pinned BARRACUDA to the equity executor seat via
    QSS, but CAMINO (the equity STRATEGIST) executed an equity SELL.
    Only the seat-holder should fire orders; this was a hard seat
    doctrine violation.

Root cause:
    SeatPolicy.evaluate() only checked `paradox_v2_seat_trusted_brains`,
    which ACCUMULATES brains over time as the operator rotates seats
    (`added_by: roster_assign_mirror`). When CAMINO was at some point
    the equity executor, its trust entry was added and never revoked.
    After the operator rotated to BARRACUDA, CAMINO was still
    "trusted" so SeatPolicy still allowed CAMINO's intents through.

Fix:
    SeatPolicy now hard-checks `brain_roster.assignments[executor]`
    (the operator's CURRENT pick) BEFORE the trust list check. Trust
    list is demoted to a soft floor / second line of defense.

This test locks the contract: only the brain that currently holds
the executor seat for a lane may produce ALLOW; every other brain
gets BLOCKED with reason `brain_not_current_seat_holder:...`.
"""
import pytest
from datetime import datetime, timezone

from db import db
from namespaces import (
    BRAIN_ROSTER,
    PARADOX_V2_SEAT_POLICY,
    PARADOX_V2_SEAT_TRUSTED,
)
from shared.pipeline.models import BrainOpinion
from shared.pipeline.seat_policy import SeatPolicy


pytestmark = pytest.mark.asyncio


def _opinion(brain_id: str, lane: str = "equity") -> BrainOpinion:
    return BrainOpinion(
        intent_id=f"tw-seat-{brain_id}-{lane}",
        brain_id=brain_id,
        lane=lane,
        symbol="MSFT" if lane == "equity" else "BTC/USD",
        action="SELL",
        confidence=0.9,
        notional_usd=10.0,
        evidence={},
    )


async def _seed_seat_policy(seat_id: str, *, enabled: bool = True) -> None:
    """Ensure a seat-policy row exists so we test the holder check, not
    the seat-missing/seat-disabled branches."""
    await db[PARADOX_V2_SEAT_POLICY].update_one(
        {"seat_id": seat_id},
        {"$set": {
            "seat_id": seat_id,
            "enabled": enabled,
            "autonomy_mode": "execute",
            "confidence_min": 0.0,
            "max_notional_usd": 100.0,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "updated_by": "pytest_tripwire",
        }},
        upsert=True,
    )


async def _seed_trust(seat_id: str, brain_id: str) -> None:
    await db[PARADOX_V2_SEAT_TRUSTED].update_one(
        {"seat_id": seat_id, "brain_id": brain_id},
        {"$set": {
            "seat_id": seat_id, "brain_id": brain_id,
            "trust_level": 1.0,
            "added_by": "pytest_tripwire",
            "added_at": datetime.now(timezone.utc).isoformat(),
        }},
        upsert=True,
    )


async def _set_roster_executor(brain_id: str | None) -> None:
    await db[BRAIN_ROSTER].update_one(
        {"_id": "current"},
        {"$set": {"assignments.executor": brain_id}},
        upsert=True,
    )


async def _restore_roster(snapshot: dict) -> None:
    await db[BRAIN_ROSTER].update_one(
        {"_id": "current"},
        {"$set": {"assignments": snapshot}},
    )


async def test_seat_policy_blocks_non_current_holder_even_if_trusted():
    """The whole point of this test: Camino is in the trust list,
    Barracuda holds the seat — Camino MUST be blocked."""
    roster_before = await db[BRAIN_ROSTER].find_one(
        {"_id": "current"}, {"_id": 0, "assignments": 1},
    )
    snapshot = ((roster_before or {}).get("assignments") or {}).copy()

    await _seed_seat_policy("equity_executor")
    # Trust BOTH brains — this is the accumulator scenario from Prod.
    await _seed_trust("equity_executor", "camino")
    await _seed_trust("equity_executor", "barracuda")
    await _set_roster_executor("barracuda")

    try:
        verdict = await SeatPolicy().evaluate(_opinion("camino"))
        assert verdict.decision == "BLOCK", (
            "Camino MUST be blocked — Barracuda is the current "
            "equity executor regardless of legacy trust entries."
        )
        assert "brain_not_current_seat_holder" in verdict.reason
        assert "camino" in verdict.reason and "barracuda" in verdict.reason

        # Sanity: the actual holder DOES pass.
        verdict2 = await SeatPolicy().evaluate(_opinion("barracuda"))
        assert verdict2.decision == "ALLOW", verdict2.reason
    finally:
        await _restore_roster(snapshot)


async def test_seat_policy_blocks_when_executor_seat_is_vacant():
    """If no brain holds the seat, NO intent passes — even from a
    trusted brain. Roster is the hard authority."""
    roster_before = await db[BRAIN_ROSTER].find_one(
        {"_id": "current"}, {"_id": 0, "assignments": 1},
    )
    snapshot = ((roster_before or {}).get("assignments") or {}).copy()

    await _seed_seat_policy("equity_executor")
    await _seed_trust("equity_executor", "barracuda")
    await _set_roster_executor(None)

    try:
        verdict = await SeatPolicy().evaluate(_opinion("barracuda"))
        assert verdict.decision == "BLOCK"
        assert verdict.reason.startswith("executor_seat_vacant:")
    finally:
        await _restore_roster(snapshot)


async def test_seat_policy_crypto_lane_also_checks_roster():
    """Same invariant for the crypto lane — uses the `crypto` roster
    key (NOT `crypto_executor`) because the canonical 8-seat IP
    names the crypto executor seat just `crypto`."""
    roster_before = await db[BRAIN_ROSTER].find_one(
        {"_id": "current"}, {"_id": 0, "assignments": 1},
    )
    snapshot = ((roster_before or {}).get("assignments") or {}).copy()

    await _seed_seat_policy("crypto_executor")
    await _seed_trust("crypto_executor", "camino")
    await _seed_trust("crypto_executor", "barracuda")
    await db[BRAIN_ROSTER].update_one(
        {"_id": "current"},
        {"$set": {"assignments.crypto": "barracuda"}},
        upsert=True,
    )

    try:
        verdict = await SeatPolicy().evaluate(_opinion("camino", lane="crypto"))
        assert verdict.decision == "BLOCK"
        assert "brain_not_current_seat_holder" in verdict.reason
    finally:
        await _restore_roster(snapshot)
