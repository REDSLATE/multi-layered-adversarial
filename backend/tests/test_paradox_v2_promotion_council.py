"""Tests for /v2/seats/pilot-readiness and /v2/council/live endpoints.

These exercise the operator-driven promotion gate (25-eval readiness
floor) and the Council Chamber live-vote surface.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone, timedelta

import pytest


pytestmark = pytest.mark.asyncio


async def _seed():
    from shared.paradox_v2.seed import seed_paradox_v2
    await seed_paradox_v2()


# ─── /v2/seats/pilot-readiness ───────────────────────────────────────


async def test_pilot_readiness_returns_all_seats_with_threshold():
    """Endpoint must return one row per seat with the 25-eval threshold."""
    from db import db
    from namespaces import PARADOX_V2_SEAT_POLICY
    from routes.paradox_v2 import get_pilot_readiness

    await _seed()
    r = await get_pilot_readiness(_user={"email": "test@x"})
    assert r["threshold"] == 25
    assert r["ladder"] == ["observe", "shadow", "toehold", "auto_execute"]

    seat_ids = {row["seat_id"] for row in r["readiness"]}
    # Must include all 4 canonical seats (2 live + 2 pilot)
    assert {"equity_executor", "crypto_executor",
            "spot_short_executor", "options_executor"} <= seat_ids


async def test_pilot_readiness_promotable_flag_off_under_threshold():
    """A pilot seat in observe mode with < 25 evals must NOT be promotable."""
    from routes.paradox_v2 import get_pilot_readiness

    await _seed()
    r = await get_pilot_readiness(_user={"email": "test@x"})
    by_id = {row["seat_id"]: row for row in r["readiness"]}

    # spot_short_executor starts observe; eval history may be 0 or low.
    # Either way, < 25 means not promotable.
    if by_id["spot_short_executor"]["eval_count"] < 25:
        assert by_id["spot_short_executor"]["promotable"] is False
        assert by_id["spot_short_executor"]["next_mode"] == "shadow"


async def test_pilot_readiness_promotable_flag_on_at_threshold():
    """Force-insert 25 BLOCKED evals on options_executor in its current
    observe window — endpoint must flip `promotable: true`."""
    from db import db
    from namespaces import PARADOX_V2_EVALUATIONS, PARADOX_V2_PROMOTION_LOG
    from routes.paradox_v2 import get_pilot_readiness

    await _seed()
    now = datetime.now(timezone.utc).isoformat()
    # Make sure the readiness window starts BEFORE our test evals.
    window_anchor = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    await db[PARADOX_V2_PROMOTION_LOG].insert_one({
        "promotion_id": str(uuid.uuid4()),
        "seat_id": "options_executor",
        "from_mode": "observe",  # synthetic — anchors the window
        "to_mode": "observe",
        "reason": "test window anchor",
        "triggered_by": "test",
        "metrics_snapshot": {},
        "ts": window_anchor,
    })

    inserted_ids: list[str] = []
    try:
        for i in range(25):
            eid = str(uuid.uuid4())
            inserted_ids.append(eid)
            await db[PARADOX_V2_EVALUATIONS].insert_one({
                "evaluation_id": eid,
                "seat_id": "options_executor",
                "opinion": {"brain_id": "chevelle", "confidence": 0.93,
                            "symbol": "SPY", "lane": "equity", "action": "BUY"},
                "decision": "BLOCKED",
                "reason": "seat_in_observe_mode: decision logged, no order placed",
                "final_notional_usd": None,
                "pipeline_trace": {},
                "ts": now,
            })

        r = await get_pilot_readiness(_user={"email": "test@x"})
        opt = next(row for row in r["readiness"] if row["seat_id"] == "options_executor")
        assert opt["eval_count"] >= 25
        assert opt["blocked_count"] >= 25
        assert opt["promotable"] is True
        assert opt["next_mode"] == "shadow"
        # avg_confidence may be pooled with pre-existing test evals in the
        # same window; just assert it's in a sane band around our 0.93 input.
        assert 0.85 <= opt["avg_confidence"] <= 0.95
    finally:
        await db[PARADOX_V2_EVALUATIONS].delete_many(
            {"evaluation_id": {"$in": inserted_ids}},
        )
        await db[PARADOX_V2_PROMOTION_LOG].delete_one({"ts": window_anchor})


async def test_pilot_readiness_roadguard_stall_blocks_promotion():
    """A RoadGuard rejection in the window must veto promotion regardless
    of eval count. Surfaces the seat as RG-stalled to the operator."""
    from db import db
    from namespaces import PARADOX_V2_EVALUATIONS, PARADOX_V2_PROMOTION_LOG
    from routes.paradox_v2 import get_pilot_readiness

    await _seed()
    now = datetime.now(timezone.utc).isoformat()
    window_anchor = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    await db[PARADOX_V2_PROMOTION_LOG].insert_one({
        "promotion_id": str(uuid.uuid4()),
        "seat_id": "spot_short_executor",
        "from_mode": "observe",
        "to_mode": "observe",
        "reason": "test window anchor",
        "triggered_by": "test",
        "metrics_snapshot": {},
        "ts": window_anchor,
    })

    inserted: list[str] = []
    try:
        # 24 clean BLOCKED + 1 ROADGUARD = 25 total but RG-stalled.
        for i in range(24):
            eid = str(uuid.uuid4())
            inserted.append(eid)
            await db[PARADOX_V2_EVALUATIONS].insert_one({
                "evaluation_id": eid,
                "seat_id": "spot_short_executor",
                "opinion": {"brain_id": "camaro", "confidence": 0.91,
                            "symbol": "NVDA", "lane": "equity", "action": "SELL"},
                "decision": "BLOCKED", "reason": "observe", "final_notional_usd": None,
                "pipeline_trace": {}, "ts": now,
            })
        eid_rg = str(uuid.uuid4())
        inserted.append(eid_rg)
        await db[PARADOX_V2_EVALUATIONS].insert_one({
            "evaluation_id": eid_rg,
            "seat_id": "spot_short_executor",
            "opinion": {"brain_id": "camaro", "confidence": 0.91,
                        "symbol": "NVDA", "lane": "equity", "action": "SELL"},
            "decision": "REJECTED_ROADGUARD",
            "reason": "daily loss limit breached",
            "final_notional_usd": None, "pipeline_trace": {}, "ts": now,
        })

        r = await get_pilot_readiness(_user={"email": "test@x"})
        ss = next(row for row in r["readiness"] if row["seat_id"] == "spot_short_executor")
        assert ss["eval_count"] >= 25
        assert ss["rejected_roadguard_count"] >= 1
        assert ss["promotable"] is False, "RG-stalled seat must not be promotable"
    finally:
        await db[PARADOX_V2_EVALUATIONS].delete_many(
            {"evaluation_id": {"$in": inserted}},
        )
        await db[PARADOX_V2_PROMOTION_LOG].delete_one({"ts": window_anchor})


async def test_pilot_readiness_auto_execute_seat_has_no_next_mode():
    """A seat already at auto_execute has nowhere to be promoted to."""
    from routes.paradox_v2 import get_pilot_readiness

    await _seed()
    r = await get_pilot_readiness(_user={"email": "test@x"})
    eq = next(row for row in r["readiness"] if row["seat_id"] == "equity_executor")
    assert eq["current_mode"] == "auto_execute"
    assert eq["next_mode"] is None
    assert eq["promotable"] is False


# ─── /v2/council/live ────────────────────────────────────────────────


async def test_council_live_returns_four_canonical_brains():
    """Endpoint must return exactly the 4 canonical brain columns in
    a deterministic order, even when no votes exist yet."""
    from routes.paradox_v2 import get_council_live

    await _seed()
    r = await get_council_live(_user={"email": "test@x"})
    chamber = r["chamber"]
    assert len(chamber) == 4
    ids = [c["brain_id"] for c in chamber]
    assert ids == ["alpha", "camaro", "chevelle", "redeye"]

    display = {c["brain_id"]: c["display_name"] for c in chamber}
    assert display["alpha"] == "Camino"
    assert display["camaro"] == "Barracuda"
    assert display["chevelle"] == "Hellcat"
    assert display["redeye"] == "GTO"


async def test_council_live_reports_latest_vote_per_brain():
    """Inject 3 votes for 'alpha' across time; latest one wins.

    NOTE: This test runs against a live DB where the brain runners may
    be inserting their own votes concurrently. We use FUTURE timestamps
    (1 hour ahead) to guarantee our injected vote is the latest.
    """
    from db import db
    from namespaces import PARADOX_V2_BRAIN_VOTES
    from routes.paradox_v2 import get_council_live

    await _seed()
    # Use times in the future so live runners can't outrace us.
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    vote_ids: list[str] = []
    try:
        for i, mins_offset in enumerate([0, 5, 10]):
            vid = str(uuid.uuid4())
            vote_ids.append(vid)
            await db[PARADOX_V2_BRAIN_VOTES].insert_one({
                "vote_id": vid,
                "brain": "alpha",
                "stance": ["HOLD", "BUY", "SELL"][i],
                "calibrated_confidence": 0.6 + i * 0.05,
                "raw_confidence": 0.8,
                "calibration_key": {"regime": "trending", "conf_bucket": 0.8},
                "memory_evidence": None,
                "negative_knowledge_triggered": False,
                "reasoning": [f"test vote {i}"],
                "timestamp": (future + timedelta(minutes=mins_offset)).isoformat(),
                "symbol": "NVDA",
                "regime": "trending",
            })

        r = await get_council_live(_user={"email": "test@x"})
        alpha_col = next(c for c in r["chamber"] if c["brain_id"] == "alpha")
        # Latest (10 min into the future) was SELL.
        assert alpha_col["latest"] is not None
        assert alpha_col["latest"]["stance"] == "SELL"
        assert alpha_col["latest"]["symbol"] == "NVDA"
    finally:
        await db[PARADOX_V2_BRAIN_VOTES].delete_many({"vote_id": {"$in": vote_ids}})


async def test_council_live_quorum_counts_recent_voters():
    """Quorum counts brains that voted in the last 10 minutes."""
    from db import db
    from namespaces import PARADOX_V2_BRAIN_VOTES
    from routes.paradox_v2 import get_council_live

    await _seed()
    now = datetime.now(timezone.utc)
    vote_ids: list[str] = []
    try:
        # Insert one recent vote for camaro only.
        vid = str(uuid.uuid4())
        vote_ids.append(vid)
        await db[PARADOX_V2_BRAIN_VOTES].insert_one({
            "vote_id": vid,
            "brain": "camaro",
            "stance": "BUY",
            "calibrated_confidence": 0.7,
            "raw_confidence": 0.75,
            "calibration_key": {"regime": "trending", "conf_bucket": 0.7},
            "memory_evidence": None,
            "negative_knowledge_triggered": False,
            "reasoning": ["recent test vote"],
            "timestamp": (now - timedelta(minutes=2)).isoformat(),
            "symbol": "AAPL",
            "regime": "trending",
        })

        r = await get_council_live(_user={"email": "test@x"})
        # 'camaro' must be in alive_in_10min (other brains may also be
        # there due to live runners — assertion is inclusive).
        assert "camaro" in r["quorum"]["alive_in_10min"]
        assert r["quorum"]["expected"] == 4
        assert r["quorum"]["alive_count"] == len(r["quorum"]["alive_in_10min"])
    finally:
        await db[PARADOX_V2_BRAIN_VOTES].delete_many({"vote_id": {"$in": vote_ids}})


async def test_council_live_handles_silent_brain():
    """A brain with no votes returns latest=None — UI distinguishes
    SILENT from HOLD."""
    from db import db
    from namespaces import PARADOX_V2_BRAIN_VOTES
    from routes.paradox_v2 import get_council_live

    await _seed()
    # Snapshot what votes the test DB already has for redeye.
    existing = await db[PARADOX_V2_BRAIN_VOTES].count_documents({"brain": "redeye"})
    if existing > 0:
        # Live test DB has data — skip rather than nuke real records.
        pytest.skip("redeye has existing votes; silent-brain path covered in integration test")
    r = await get_council_live(_user={"email": "test@x"})
    red = next(c for c in r["chamber"] if c["brain_id"] == "redeye")
    assert red["latest"] is None
