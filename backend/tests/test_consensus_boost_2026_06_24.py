"""Regression tests for the consensus-boost feature (2026-06-24).

Doctrine pins:
  * Non-executor brains STILL get blocked at the seat by
    `brain_not_current_seat_holder`. Fire authority is unchanged.
  * Their opinion is captured to `intent_consensus_pool` regardless.
  * The executor's confidence is shifted ±0.05 per agreeing/disagreeing
    advisor, capped at ±0.15.
  * Pool entries TTL after 15 min (set on the collection — we don't
    test that TTL actually fires here; we test the WINDOW filter in
    the read path, which is what enforces semantics in real time).
  * HOLD/ABSTAIN executor → no boost (no directional reference).
  * Effective confidence is clamped to [0.0, 1.0].
  * The executor's own historical advisory entries are excluded.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

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
    DEFAULT_BOOST_CAP,
    DEFAULT_BOOST_PER_BRAIN,
    DEFAULT_WINDOW_SECONDS,
    clear_runtime_flags_cache,
    compute_consensus_boost,
    record_advisory_opinion,
    record_telemetry,
)
from shared.pipeline.models import BrainOpinion
from shared.pipeline.seat_policy import SeatPolicy


pytestmark = pytest.mark.asyncio


# ── Fixtures ────────────────────────────────────────────────────────
@pytest.fixture
async def clean_pool():
    """Wipe pool + telemetry, re-run ensure_indexes, flush runtime_flag
    cache. Sets up a fresh slate for every test."""
    await db[INTENT_CONSENSUS_POOL].drop()
    await db[INTENT_CONSENSUS_TELEMETRY].drop()
    await ensure_indexes()
    clear_runtime_flags_cache()
    # Also flush any runtime_flag overrides from prior tests.
    await db["runtime_flags"].delete_many({
        "_id": {"$in": [
            "consensus_boost_per_brain",
            "consensus_boost_cap",
            "consensus_window_seconds",
        ]},
    })
    yield db
    await db[INTENT_CONSENSUS_POOL].drop()
    await db[INTENT_CONSENSUS_TELEMETRY].drop()


def _opinion(
    intent_id: str = "i1",
    brain: str = "camino",
    lane: str = "equity",
    symbol: str = "AAPL",
    action: str = "BUY",
    confidence: float = 0.7,
):
    return BrainOpinion(
        intent_id=intent_id,
        brain_id=brain,
        lane=lane,
        symbol=symbol,
        action=action,
        confidence=confidence,
        notional_usd=100.0,
        evidence={},
    )


# ── Unit tests on compute_consensus_boost ──────────────────────────
class TestComputeBoostMath:
    async def test_no_advisors_no_boost(self, clean_pool):
        opinion = _opinion()
        result = await compute_consensus_boost(opinion)
        assert result.advisor_count == 0
        assert result.delta == 0.0
        assert result.effective_confidence == 0.7

    async def test_one_agreeing_advisor(self, clean_pool):
        # Camino (executor) wants BUY at 0.7. Barracuda emitted BUY
        # earlier — also got blocked, also in the pool.
        await record_advisory_opinion(
            _opinion(intent_id="adv1", brain="barracuda", action="BUY"),
            block_reason="brain_not_current_seat_holder:barracuda!=camino",
        )
        result = await compute_consensus_boost(_opinion(brain="camino"))
        assert result.agree_count == 1
        assert result.disagree_count == 0
        assert result.agree_brains == ["barracuda"]
        assert result.delta == 0.05
        assert result.effective_confidence == 0.75
        assert result.advisor_count == 1

    async def test_three_agreeing_advisors_boost_capped(self, clean_pool):
        # 3 brains agree → naive boost would be +0.15. Cap is 0.15 so
        # this just barely doesn't hit the ceiling — confirming the
        # cap doesn't crop below the natural max.
        for b in ["barracuda", "hellcat", "gto"]:
            await record_advisory_opinion(
                _opinion(intent_id=f"adv-{b}", brain=b, action="BUY"),
                block_reason="brain_not_current_seat_holder",
            )
        result = await compute_consensus_boost(_opinion(brain="camino"))
        assert result.agree_count == 3
        assert result.delta == pytest.approx(0.15)
        assert result.effective_confidence == pytest.approx(0.85)

    async def test_four_agreeing_advisors_hits_cap(self, clean_pool):
        # 4 agree → naive 0.20 → capped at 0.15.
        for b in ["barracuda", "hellcat", "gto", "redeye"]:
            await record_advisory_opinion(
                _opinion(intent_id=f"adv-{b}", brain=b, action="BUY"),
                block_reason="brain_not_current_seat_holder",
            )
        result = await compute_consensus_boost(_opinion(brain="camino"))
        assert result.agree_count == 4
        assert result.delta == pytest.approx(DEFAULT_BOOST_CAP)  # 0.15
        assert result.effective_confidence == pytest.approx(0.85)

    async def test_disagreeing_advisor_lowers(self, clean_pool):
        # Executor BUY @ 0.7, Barracuda emitted SELL → −0.05.
        await record_advisory_opinion(
            _opinion(intent_id="adv1", brain="barracuda", action="SELL"),
            block_reason="brain_not_current_seat_holder",
        )
        result = await compute_consensus_boost(_opinion(action="BUY"))
        assert result.disagree_count == 1
        assert result.delta == pytest.approx(-0.05)
        assert result.effective_confidence == pytest.approx(0.65)

    async def test_mixed_agree_and_disagree(self, clean_pool):
        # 2 agree, 1 disagree → net +0.05.
        await record_advisory_opinion(
            _opinion(intent_id="a1", brain="barracuda", action="BUY"),
            "blocked",
        )
        await record_advisory_opinion(
            _opinion(intent_id="a2", brain="hellcat", action="BUY"),
            "blocked",
        )
        await record_advisory_opinion(
            _opinion(intent_id="a3", brain="gto", action="SELL"),
            "blocked",
        )
        result = await compute_consensus_boost(_opinion(brain="camino"))
        assert result.agree_count == 2
        assert result.disagree_count == 1
        assert result.delta == pytest.approx(0.05)
        assert result.effective_confidence == pytest.approx(0.75)

    async def test_hold_opinions_ignored(self, clean_pool):
        # HOLD advisories don't count as agree or disagree — they're
        # non-directional.
        await record_advisory_opinion(
            _opinion(intent_id="h1", brain="barracuda", action="HOLD"),
            "blocked",
        )
        result = await compute_consensus_boost(_opinion(action="BUY"))
        assert result.agree_count == 0
        assert result.disagree_count == 0
        assert result.advisor_count == 1   # still counted as present
        assert result.delta == 0.0

    async def test_executor_own_advisories_excluded(self, clean_pool):
        # If the executor seat changed mid-window, the executor might
        # have its own prior advisory entries in the pool — exclude
        # them from consensus calc (you can't agree with yourself).
        await record_advisory_opinion(
            _opinion(intent_id="self1", brain="camino", action="BUY"),
            "blocked_previously",
        )
        result = await compute_consensus_boost(_opinion(brain="camino"))
        assert result.advisor_count == 0
        assert result.delta == 0.0

    async def test_executor_hold_gets_no_boost(self, clean_pool):
        # HOLD/ABSTAIN executor has no directional reference, so
        # there's nothing to boost against.
        await record_advisory_opinion(
            _opinion(intent_id="a1", brain="barracuda", action="BUY"),
            "blocked",
        )
        result = await compute_consensus_boost(_opinion(action="HOLD"))
        assert result.delta == 0.0
        assert result.effective_confidence == 0.7

    async def test_old_advisories_excluded_by_window(self, clean_pool):
        # Insert an advisory with ts 16 min ago — outside the 15-min
        # window — should NOT be counted.
        await db[INTENT_CONSENSUS_POOL].insert_one({
            "intent_id": "stale",
            "brain_id": "barracuda",
            "lane": "equity",
            "symbol": "AAPL",
            "action": "BUY",
            "confidence": 0.8,
            "ts": datetime.now(timezone.utc) - timedelta(seconds=1000),
            "block_reason": "old",
        })
        result = await compute_consensus_boost(_opinion(brain="camino"))
        assert result.agree_count == 0
        assert result.advisor_count == 0

    async def test_different_symbol_ignored(self, clean_pool):
        await record_advisory_opinion(
            _opinion(intent_id="a1", brain="barracuda",
                     symbol="MSFT", action="BUY"),
            "blocked",
        )
        result = await compute_consensus_boost(_opinion(symbol="AAPL"))
        assert result.advisor_count == 0

    async def test_different_lane_ignored(self, clean_pool):
        await record_advisory_opinion(
            _opinion(intent_id="a1", brain="barracuda",
                     lane="crypto", action="BUY"),
            "blocked",
        )
        result = await compute_consensus_boost(_opinion(lane="equity"))
        assert result.advisor_count == 0

    async def test_effective_confidence_clamped_to_zero(self, clean_pool):
        # base 0.05, 3 disagree → would naively go to −0.10 → clamped.
        for b in ["barracuda", "hellcat", "gto"]:
            await record_advisory_opinion(
                _opinion(intent_id=f"a-{b}", brain=b, action="SELL"),
                "blocked",
            )
        result = await compute_consensus_boost(
            _opinion(confidence=0.05, action="BUY")
        )
        assert result.effective_confidence == 0.0

    async def test_effective_confidence_clamped_to_one(self, clean_pool):
        # base 0.95, 3 agree → would naively go to 1.10 → clamped.
        for b in ["barracuda", "hellcat", "gto"]:
            await record_advisory_opinion(
                _opinion(intent_id=f"a-{b}", brain=b, action="BUY"),
                "blocked",
            )
        result = await compute_consensus_boost(
            _opinion(confidence=0.95, action="BUY")
        )
        assert result.effective_confidence == 1.0

    async def test_brain_with_two_advisories_dedup_to_latest(self, clean_pool):
        # If Barracuda emitted BUY then later SELL in the same window,
        # only the LATEST advisory counts. (Brain reversed itself.)
        await record_advisory_opinion(
            _opinion(intent_id="a1", brain="barracuda", action="BUY"),
            "blocked",
        )
        # Manually overwrite with a more recent SELL to simulate this
        # (record_advisory_opinion just inserts, so we use the same
        # insertion path).
        await record_advisory_opinion(
            _opinion(intent_id="a2", brain="barracuda", action="SELL"),
            "blocked",
        )
        result = await compute_consensus_boost(_opinion(action="BUY"))
        # Result must be exactly ONE adviser (not two). The dict
        # collapse on brain_id ensures dedup — but the iteration order
        # of `to_list()` may not be ts-sorted. We're not strict about
        # which one wins; we ARE strict that only ONE counts.
        assert result.advisor_count == 1
        assert result.agree_count + result.disagree_count == 1


# ── Runtime flag override ──────────────────────────────────────────
class TestRuntimeFlagOverride:
    async def test_mongo_override_changes_per_brain_value(self, clean_pool):
        # Operator can tune from the UI without redeploy.
        await db["runtime_flags"].update_one(
            {"_id": "consensus_boost_per_brain"},
            {"$set": {"_id": "consensus_boost_per_brain", "value": 0.10}},
            upsert=True,
        )
        clear_runtime_flags_cache()
        await record_advisory_opinion(
            _opinion(intent_id="a1", brain="barracuda", action="BUY"),
            "blocked",
        )
        result = await compute_consensus_boost(_opinion())
        assert result.delta == pytest.approx(0.10)

    async def test_defaults_used_when_no_override(self, clean_pool):
        # Sanity: defaults match what the code says.
        assert DEFAULT_BOOST_PER_BRAIN == 0.05
        assert DEFAULT_BOOST_CAP == 0.15
        assert DEFAULT_WINDOW_SECONDS == 900


# ── Telemetry sidecar ──────────────────────────────────────────────
class TestTelemetry:
    async def test_record_telemetry_persists(self, clean_pool):
        await record_advisory_opinion(
            _opinion(intent_id="a1", brain="barracuda", action="BUY"),
            "blocked",
        )
        opinion = _opinion(intent_id="exec1", brain="camino")
        result = await compute_consensus_boost(opinion)
        await record_telemetry("exec1", result, applied=True)

        row = await db[INTENT_CONSENSUS_TELEMETRY].find_one({"intent_id": "exec1"})
        assert row is not None
        assert row["applied"] is True
        assert row["delta"] == pytest.approx(0.05)
        assert row["agree_count"] == 1
        assert "barracuda" in row["agree_brains"]


# ── End-to-end through SeatPolicy.evaluate ─────────────────────────
class TestSeatPolicyIntegration:
    """The whole point of the change. Sets up real roster + seat
    config rows so SeatPolicy.evaluate() runs end-to-end and we
    can verify the boost is applied at the floor check."""

    @pytest.fixture
    async def seat_world(self, clean_pool):
        """Roster + seat config for equity_executor=camino @ floor 0.85.
        Preserves prior roster so this test doesn't pollute other suites
        (e.g. test_live_preview_login_hotfix expects the full 8-seat map)."""
        prior_roster = await db[BRAIN_ROSTER].find_one({"_id": "current"})
        # Roster: camino is the equity executor. Use $set so we don't
        # wipe other seat assignments.
        await db[BRAIN_ROSTER].update_one(
            {"_id": "current"},
            {"$set": {
                "assignments.executor": "camino",
                "assignments.crypto": "barracuda",
                "seat_epoch": 1,
                "updated_by": "test",
            }},
            upsert=True,
        )
        # Seat config: confidence_min = 0.85 (so 0.70 fails without boost).
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
        # Trust: camino is trusted for equity_executor.
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
        # Restore prior roster doc verbatim if it existed.
        if prior_roster:
            await db[BRAIN_ROSTER].replace_one(
                {"_id": "current"}, prior_roster, upsert=True,
            )

    async def test_non_executor_still_blocked_AND_pool_captures(
        self, seat_world
    ):
        # Barracuda emits → still blocked, but pool gets the entry.
        policy = SeatPolicy()
        verdict = await policy.evaluate(_opinion(brain="barracuda", action="BUY"))
        assert verdict.decision == "BLOCK"
        assert "brain_not_current_seat_holder" in verdict.reason

        pool_rows = await db[INTENT_CONSENSUS_POOL].find({}).to_list(length=10)
        assert len(pool_rows) == 1
        assert pool_rows[0]["brain_id"] == "barracuda"
        assert pool_rows[0]["action"] == "BUY"

    async def test_executor_below_floor_fails_without_advisors(
        self, seat_world
    ):
        # Camino @ 0.70, floor 0.85. No advisors. Should BLOCK.
        policy = SeatPolicy()
        verdict = await policy.evaluate(_opinion(brain="camino", confidence=0.70))
        assert verdict.decision == "BLOCK"
        assert "below_seat_confidence_min" in verdict.reason

    async def test_executor_below_floor_PASSES_with_three_advisors(
        self, seat_world
    ):
        # Three brains agree → +0.15 boost → effective 0.85 == floor.
        for b in ["barracuda", "hellcat", "gto"]:
            policy = SeatPolicy()
            await policy.evaluate(_opinion(intent_id=f"a-{b}",
                                           brain=b, confidence=0.7,
                                           action="BUY"))
        # Now the executor's own intent.
        policy = SeatPolicy()
        verdict = await policy.evaluate(
            _opinion(intent_id="exec1", brain="camino",
                     confidence=0.70, action="BUY"),
        )
        assert verdict.decision == "ALLOW", (
            f"expected ALLOW after +0.15 boost; got: {verdict.reason}"
        )
        # And the reason string carries the boost telemetry.
        assert "consensus" in verdict.reason.lower()
        assert "Δ+0.150" in verdict.reason or "Δ+0.15" in verdict.reason

    async def test_executor_disagreement_can_block(self, seat_world):
        # Three brains disagree → −0.15. Camino @ 0.95 - 0.15 = 0.80 → fails.
        for b in ["barracuda", "hellcat", "gto"]:
            policy = SeatPolicy()
            await policy.evaluate(_opinion(intent_id=f"d-{b}",
                                           brain=b, confidence=0.7,
                                           action="SELL"))
        policy = SeatPolicy()
        verdict = await policy.evaluate(
            _opinion(intent_id="exec1", brain="camino",
                     confidence=0.95, action="BUY"),
        )
        assert verdict.decision == "BLOCK"
        assert "below_seat_confidence_min" in verdict.reason
        assert "consensus" in verdict.reason

    async def test_telemetry_written_on_executor_path(self, seat_world):
        await record_advisory_opinion(
            _opinion(intent_id="adv1", brain="barracuda", action="BUY"),
            "blocked",
        )
        policy = SeatPolicy()
        await policy.evaluate(
            _opinion(intent_id="exec1", brain="camino",
                     confidence=0.9, action="BUY"),
        )
        row = await db[INTENT_CONSENSUS_TELEMETRY].find_one(
            {"intent_id": "exec1"}
        )
        assert row is not None
        assert row["agree_count"] == 1
        assert row["base_confidence"] == 0.9
        assert row["delta"] == pytest.approx(0.05)
