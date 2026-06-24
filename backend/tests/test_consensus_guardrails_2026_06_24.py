"""Operator-pinned guardrail regressions for the consensus boost
(2026-06-24, pass 2).

Two guardrails the operator pinned:

  1. **Advisor boost never bypasses RoadGuard.**
     RoadGuard checks trading_controls_disabled / zero_notional /
     market_closed / insufficient_buying_power / duplicate_order —
     none of these consume `confidence`. A boosted-past-seat-floor
     intent must still clear every RoadGuard stop on its own merit.
     This file pins that with explicit positive AND negative cases.

  2. **Receipts must stamp the five operator-named provenance fields:**
       base_confidence
       advisor_boost
       effective_confidence
       advisor_votes_used
       advisor_window_seconds
     So when an operator opens a receipt for a trade that passed
     because of consensus, they see EXACTLY why.

Files this exercises:
  - shared/pipeline/execution_pipeline.py  (receipt construction)
  - shared/pipeline/seat_policy.py         (SeatVerdict.consensus)
  - shared/pipeline/consensus_pool.py      (5-field result shape)
  - shared/pipeline/roadguard.py           (untouched — proves the
                                            architectural separation)
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict

import pytest

from db import db, ensure_indexes
from namespaces import (
    INTENT_CONSENSUS_POOL,
    INTENT_CONSENSUS_TELEMETRY,
    BRAIN_ROSTER,
    PARADOX_V2_SEAT_POLICY,
    PARADOX_V2_SEAT_TRUSTED,
)
from shared.pipeline.consensus_pool import (
    clear_runtime_flags_cache,
    record_advisory_opinion,
)
from shared.pipeline.execution_pipeline import run_execution_pipeline
from shared.pipeline.governor import Governor
from shared.pipeline.models import BrainOpinion
from shared.pipeline.receipts import ReceiptStore, PIPELINE_RECEIPTS_COLL
from shared.pipeline.roadguard import RoadGuard
from shared.pipeline.seat_policy import SeatPolicy


pytestmark = pytest.mark.asyncio


# ── Stub brokers ────────────────────────────────────────────────────
class _AcceptAllBroker:
    """Pretends the broker always succeeds. Used to drive the happy
    path so we can verify receipts carry consensus on SUBMITTED rows."""

    def __init__(self):
        self.calls = []

    async def submit_market_order(self, *, symbol, side, notional_usd, lane):
        self.calls.append({"symbol": symbol, "side": side,
                           "notional_usd": notional_usd, "lane": lane})
        return {"status": "submitted", "ref": "test-ref"}


class _UnreachableBroker:
    """If RoadGuard correctly blocks, the broker MUST NOT be called.
    This stub raises if anyone tries to talk to it — making 'broker
    was reached' a hard test assertion."""

    async def submit_market_order(self, **kwargs):
        raise AssertionError(
            "RoadGuard let the call through and the broker was reached — "
            f"this should be IMPOSSIBLE for a roadguard-block scenario. "
            f"args={kwargs}"
        )


# ── Fixtures ────────────────────────────────────────────────────────
@pytest.fixture
async def seat_world():
    """Operator's prod roster + a strict seat that requires conf>=0.85.
    Cleans pool/telemetry/receipts; restores prior roster on teardown.
    """
    await db[INTENT_CONSENSUS_POOL].drop()
    await db[INTENT_CONSENSUS_TELEMETRY].drop()
    await db[PIPELINE_RECEIPTS_COLL].drop()
    await ensure_indexes()
    clear_runtime_flags_cache()
    await db["runtime_flags"].delete_many({
        "_id": {"$in": [
            "consensus_boost_per_brain",
            "consensus_boost_cap",
            "consensus_window_seconds",
        ]},
    })

    # Read prior roster doc to restore on teardown — DO NOT replace_one.
    prior_roster = await db[BRAIN_ROSTER].find_one({"_id": "current"})

    # Surgical $set so we don't nuke the other 6 seats.
    await db[BRAIN_ROSTER].update_one(
        {"_id": "current"},
        {"$set": {
            "assignments.executor": "camino",
            "assignments.crypto": "barracuda",
        }},
        upsert=True,
    )
    await db[PARADOX_V2_SEAT_POLICY].replace_one(
        {"seat_id": "equity_executor"},
        {
            "seat_id": "equity_executor",
            "enabled": True,
            "autonomy_mode": "auto_execute",
            "confidence_min": 0.85,
            "max_notional_usd": 500.0,
        },
        upsert=True,
    )
    await db[PARADOX_V2_SEAT_TRUSTED].replace_one(
        {"seat_id": "equity_executor", "brain_id": "camino"},
        {
            "seat_id": "equity_executor",
            "brain_id": "camino",
            "trusted_at": datetime.now(timezone.utc).isoformat(),
        },
        upsert=True,
    )

    yield

    # Restore prior roster verbatim so we don't pollute future tests
    # (this is the bug iteration_7's testing agent caught and fixed).
    await db[INTENT_CONSENSUS_POOL].drop()
    await db[INTENT_CONSENSUS_TELEMETRY].drop()
    await db[PIPELINE_RECEIPTS_COLL].drop()
    if prior_roster is not None:
        await db[BRAIN_ROSTER].replace_one(
            {"_id": "current"}, prior_roster, upsert=True
        )


def _opinion(
    *,
    intent_id: str = "exec1",
    brain: str = "camino",
    lane: str = "equity",
    symbol: str = "AAPL",
    action: str = "BUY",
    confidence: float = 0.70,
    notional_usd: float = 250.0,
) -> BrainOpinion:
    return BrainOpinion(
        intent_id=intent_id,
        brain_id=brain,
        lane=lane,
        symbol=symbol,
        action=action,
        confidence=confidence,
        notional_usd=notional_usd,
        evidence={},
    )


async def _seed_three_agreeing_advisors(action: str = "BUY"):
    """Push 3 non-executor BUY advisories into the pool — at default
    +0.05/brain this gives a +0.15 boost (cap)."""
    for b in ["barracuda", "hellcat", "gto"]:
        await record_advisory_opinion(
            _opinion(intent_id=f"adv-{b}", brain=b, action=action),
            block_reason="brain_not_current_seat_holder",
        )


async def _enable_trading():
    # The trading-controls module reads `trading_controls.enabled`
    # (NOT `trading_runtime` — that's a different collection).
    await db["trading_controls"].update_one(
        {"_id": "current"},
        {"$set": {"enabled": True, "ts": datetime.now(timezone.utc).isoformat()}},
        upsert=True,
    )


async def _disable_trading():
    await db["trading_controls"].update_one(
        {"_id": "current"},
        {"$set": {"enabled": False, "ts": datetime.now(timezone.utc).isoformat()}},
        upsert=True,
    )


# ── Guardrail #1: Boost cannot bypass RoadGuard ────────────────────
class TestRoadGuardCannotBeBypassedByBoost:
    """Operator pin (2026-06-24):
       "Advisor boost never bypasses RoadGuard."
    """

    async def test_zero_notional_still_blocks_even_with_full_boost(
        self, seat_world
    ):
        """Even with +0.15 boost moving the seat past the floor, if
        the final notional collapses to zero, RoadGuard MUST still
        block at `zero_notional`. The receipt's restriction_source
        MUST read 'roadguard' (not 'seat')."""
        await _enable_trading()
        await _seed_three_agreeing_advisors("BUY")

        # Use crypto lane so the market_closed check doesn't fire on
        # weekends — keeps the test deterministic.
        # ALSO use a notional that the governor will scale down past
        # zero. Easier: just pass notional=0 directly.
        opinion = _opinion(
            intent_id="exec1", brain="barracuda",  # crypto exec
            lane="crypto", symbol="ETHUSD",
            action="BUY", confidence=0.70, notional_usd=0.0,
        )
        # Need a crypto-executor seat policy row for barracuda.
        await db[PARADOX_V2_SEAT_POLICY].replace_one(
            {"seat_id": "crypto_executor"},
            {
                "seat_id": "crypto_executor", "enabled": True,
                "autonomy_mode": "auto_execute",
                "confidence_min": 0.85, "max_notional_usd": 500.0,
            },
            upsert=True,
        )
        await db[PARADOX_V2_SEAT_TRUSTED].replace_one(
            {"seat_id": "crypto_executor", "brain_id": "barracuda"},
            {"seat_id": "crypto_executor", "brain_id": "barracuda",
             "trusted_at": datetime.now(timezone.utc).isoformat()},
            upsert=True,
        )
        # Seed crypto-lane advisors that would boost barracuda.
        for b in ["camino", "hellcat", "gto"]:
            await record_advisory_opinion(
                _opinion(intent_id=f"cadv-{b}", brain=b,
                         lane="crypto", symbol="ETHUSD",
                         action="BUY", confidence=0.6),
                block_reason="brain_not_current_seat_holder",
            )

        receipt = await run_execution_pipeline(
            opinion,
            seat_policy=SeatPolicy(),
            governor=Governor(),
            roadguard=RoadGuard(),
            broker=_UnreachableBroker(),    # MUST not be called
            receipt_store=ReceiptStore(),
        )
        assert receipt.final_status == "BLOCKED"
        assert receipt.restriction_source == "roadguard"
        assert "zero_notional" in receipt.final_reason
        # And the consensus is STILL stamped on the receipt — we want
        # the operator to see the seat decided ALLOW based on boost
        # and that RoadGuard then said NO.
        assert receipt.consensus is not None
        assert receipt.consensus["agree_count"] == 3
        assert receipt.consensus["advisor_boost"] == pytest.approx(0.15)

    async def test_trading_controls_disabled_still_blocks_with_full_boost(
        self, seat_world
    ):
        """Operator kill switch outranks consensus boost. Period.
        Boosted-past-floor intent still gets killed by the operator's
        Mongo flag with `trading_controls_disabled`."""
        await _disable_trading()
        await _seed_three_agreeing_advisors("BUY")

        opinion = _opinion(
            intent_id="exec1", brain="camino",
            action="BUY", confidence=0.70, notional_usd=250.0,
        )
        receipt = await run_execution_pipeline(
            opinion,
            seat_policy=SeatPolicy(),
            governor=Governor(),
            roadguard=RoadGuard(),
            broker=_UnreachableBroker(),
            receipt_store=ReceiptStore(),
        )
        assert receipt.final_status == "BLOCKED"
        assert receipt.restriction_source == "roadguard"
        assert receipt.final_reason == "trading_controls_disabled"

        # Re-enable for the rest of the suite.
        await _enable_trading()


# ── Guardrail #2: Receipts stamp the operator-named provenance ─────
class TestReceiptCarriesConsensusProvenance:
    """Operator pin (2026-06-24): receipts must stamp
       base_confidence, advisor_boost, effective_confidence,
       advisor_votes_used, advisor_window_seconds.
    Every receipt that reaches the seat layer carries them — whether
    the seat allowed, the roadguard blocked, or the broker submitted.
    """

    REQUIRED_FIELDS = (
        "base_confidence",
        "advisor_boost",
        "effective_confidence",
        "advisor_votes_used",
        "advisor_window_seconds",
    )

    def _assert_all_provenance_present(self, consensus: Dict[str, Any]):
        for f in self.REQUIRED_FIELDS:
            assert f in consensus, (
                f"receipt.consensus missing operator-pinned field {f!r}; "
                f"got: {sorted(consensus.keys())}"
            )

    async def test_submitted_receipt_carries_full_provenance(self, seat_world):
        """Happy path: 3 agree → boost → seat allows → broker submits.
        Receipt MUST contain all 5 provenance fields, agree_brains list,
        and applied=True."""
        await _enable_trading()
        # Use crypto path to skip equity market-hours.
        await db[PARADOX_V2_SEAT_POLICY].replace_one(
            {"seat_id": "crypto_executor"},
            {"seat_id": "crypto_executor", "enabled": True,
             "autonomy_mode": "auto_execute",
             "confidence_min": 0.85, "max_notional_usd": 500.0},
            upsert=True,
        )
        await db[PARADOX_V2_SEAT_TRUSTED].replace_one(
            {"seat_id": "crypto_executor", "brain_id": "barracuda"},
            {"seat_id": "crypto_executor", "brain_id": "barracuda",
             "trusted_at": datetime.now(timezone.utc).isoformat()},
            upsert=True,
        )
        for b in ["camino", "hellcat", "gto"]:
            await record_advisory_opinion(
                _opinion(intent_id=f"adv-{b}", brain=b,
                         lane="crypto", symbol="ETHUSD",
                         action="BUY", confidence=0.6),
                block_reason="brain_not_current_seat_holder",
            )

        opinion = _opinion(
            intent_id="exec1", brain="barracuda",
            lane="crypto", symbol="ETHUSD",
            action="BUY", confidence=0.70, notional_usd=250.0,
        )
        broker = _AcceptAllBroker()
        receipt = await run_execution_pipeline(
            opinion,
            seat_policy=SeatPolicy(),
            governor=Governor(),
            roadguard=RoadGuard(),
            broker=broker,
            receipt_store=ReceiptStore(),
        )
        assert receipt.final_status == "SUBMITTED"
        assert receipt.consensus is not None
        self._assert_all_provenance_present(receipt.consensus)
        assert receipt.consensus["base_confidence"] == 0.70
        assert receipt.consensus["advisor_boost"] == pytest.approx(0.15)
        assert receipt.consensus["effective_confidence"] == pytest.approx(0.85)
        assert receipt.consensus["advisor_votes_used"] == 3
        assert receipt.consensus["advisor_window_seconds"] == 900
        assert receipt.consensus["agree_brains"] == ["camino", "gto", "hellcat"]
        assert len(broker.calls) == 1   # broker WAS called (happy path)

    async def test_seat_blocked_receipt_carries_provenance(self, seat_world):
        """Below-floor with disagreeing advisors → seat blocks →
        receipt's consensus still surfaces the 5 fields so operator
        can see WHY the floor wasn't reached."""
        await _enable_trading()
        # 3 brains disagree → base 0.85 - 0.15 = 0.70 < floor 0.85.
        for b in ["barracuda", "hellcat", "gto"]:
            await record_advisory_opinion(
                _opinion(intent_id=f"d-{b}", brain=b, action="SELL"),
                block_reason="brain_not_current_seat_holder",
            )
        opinion = _opinion(confidence=0.85, action="BUY")
        receipt = await run_execution_pipeline(
            opinion,
            seat_policy=SeatPolicy(),
            governor=Governor(),
            roadguard=RoadGuard(),
            broker=_UnreachableBroker(),
            receipt_store=ReceiptStore(),
        )
        assert receipt.final_status == "BLOCKED"
        assert receipt.restriction_source == "seat"
        assert "below_seat_confidence_min" in receipt.final_reason
        assert receipt.consensus is not None
        self._assert_all_provenance_present(receipt.consensus)
        assert receipt.consensus["disagree_count"] == 3
        assert receipt.consensus["advisor_boost"] == pytest.approx(-0.15)

    async def test_zero_advisors_zero_boost_but_provenance_still_present(
        self, seat_world
    ):
        """Even with no advisors in the window, the receipt MUST
        stamp the 5 fields (so post-mortem code can rely on shape
        stability — operator's UI should never crash on a missing
        key)."""
        await _enable_trading()
        opinion = _opinion(confidence=0.90, action="BUY")
        broker = _AcceptAllBroker()
        receipt = await run_execution_pipeline(
            opinion,
            seat_policy=SeatPolicy(),
            governor=Governor(),
            roadguard=RoadGuard(),
            broker=broker,
            receipt_store=ReceiptStore(),
        )
        # NOTE: this may or may not submit depending on market hours.
        # The shape contract is what we're pinning here — not the
        # submit outcome. Skip the broker assertion.
        assert receipt.consensus is not None, (
            "receipt.consensus must be stamped even on zero-advisor runs"
        )
        self._assert_all_provenance_present(receipt.consensus)
        assert receipt.consensus["advisor_boost"] == 0.0
        assert receipt.consensus["advisor_votes_used"] == 0
        assert receipt.consensus["advisor_window_seconds"] == 900
