"""Tests for the /admin/auto-submit/tunables-simulator endpoint.

The simulator answers "if I lowered confidence_min from 0.85 → 0.75,
how many intents would I actually unlock?" before the operator
commits to a policy change. These tests seed synthetic skips + intents
in Mongo and verify the simulator's math matches.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

pytestmark = pytest.mark.asyncio


async def _reset_state():
    from db import db
    from namespaces import SHARED_GATE_RESULTS, SHARED_INTENTS
    from shared.auto_submit_policy import reset_policy_for_tests
    reset_policy_for_tests()
    # Wipe only test fixtures (tagged with marker) so we don't blow up
    # the live preview database.
    await db[SHARED_GATE_RESULTS].delete_many({"reason": {"$regex": "^TUNABLES_TEST"}})
    await db[SHARED_INTENTS].delete_many({"symbol": {"$regex": "^TUNABLES_"}})


async def _seed_skip(intent_id: str, category: str, *, confidence: float = 0.5,
                    symbol: str = "TUNABLES_NVDA", lane: str = "equity",
                    action: str = "BUY", stack: str = "alpha") -> None:
    """Insert a paired skipped-intent (in shared_intents) + skip row
    (in shared_gate_results) so the simulator's join produces a row."""
    from db import db
    from namespaces import SHARED_GATE_RESULTS, SHARED_INTENTS

    now = datetime.now(timezone.utc).isoformat()
    await db[SHARED_INTENTS].insert_one({
        "intent_id": intent_id, "symbol": symbol, "lane": lane,
        "action": action, "stack": stack, "confidence": confidence,
        "ingest_ts": now,
    })
    await db[SHARED_GATE_RESULTS].insert_one({
        "intent_id": intent_id, "kind": "auto_submit_skipped",
        "skip_category": category, "ts": now,
        "reason": f"TUNABLES_TEST {category}",
    })


async def test_confidence_what_if_unlocks_only_passing_skips():
    """Seed three low_confidence skips at 0.78, 0.72, 0.55. Lowering
    floor to 0.75 must unlock exactly the 0.78 one, etc."""
    from routes.admin_auto_submit import tunables_simulator
    await _reset_state()

    ids = [str(uuid.uuid4()) for _ in range(3)]
    try:
        await _seed_skip(ids[0], "low_confidence", confidence=0.78, symbol="TUNABLES_NVDA")
        await _seed_skip(ids[1], "low_confidence", confidence=0.72, symbol="TUNABLES_NVDA")
        await _seed_skip(ids[2], "low_confidence", confidence=0.55, symbol="TUNABLES_AAL")

        r = await tunables_simulator(_user={"email": "test@x"}, hours=1)
        rows = {x["new_min"]: x for x in r["confidence_what_if"]}

        # Lowering to 0.75 unlocks only the 0.78 row.
        assert rows[0.75]["would_unlock"] == 1
        # Lowering to 0.70 unlocks the 0.78 + 0.72 rows.
        assert rows[0.70]["would_unlock"] == 2
        # Lowering to 0.60 still doesn't unlock the 0.55 row.
        assert rows[0.60]["would_unlock"] == 2
        # Symbol breakdown: NVDA is the top one for floors ≤ 0.70.
        assert rows[0.70]["top_symbols"][0][0] == "TUNABLES_NVDA"
    finally:
        await _reset_state()


async def test_lane_what_if_groups_by_lane():
    """Two lane-filtered skips on `options` lane, one on `futures`.
    Simulator must group by lane and sort by count desc."""
    from routes.admin_auto_submit import tunables_simulator
    await _reset_state()

    ids = [str(uuid.uuid4()) for _ in range(3)]
    try:
        await _seed_skip(ids[0], "lane_filtered", lane="options",
                         symbol="TUNABLES_SPY")
        await _seed_skip(ids[1], "lane_filtered", lane="options",
                         symbol="TUNABLES_QQQ")
        await _seed_skip(ids[2], "lane_filtered", lane="futures",
                         symbol="TUNABLES_ES")

        r = await tunables_simulator(_user={"email": "test@x"}, hours=1)
        # `equity` + `crypto` are in default allowed_lanes so they
        # shouldn't show up. `options` (×2) > `futures` (×1).
        lanes = r["lane_what_if"]
        assert lanes[0]["lane"] == "options"
        assert lanes[0]["would_unlock"] == 2
        assert lanes[1]["lane"] == "futures"
        assert lanes[1]["would_unlock"] == 1
    finally:
        await _reset_state()


async def test_action_what_if_skips_hold():
    """HOLD action skips are intentional design — adding HOLD to
    allowed_actions makes no sense (no order to place). Simulator
    must NOT suggest unlocking HOLD intents."""
    from routes.admin_auto_submit import tunables_simulator
    await _reset_state()

    ids = [str(uuid.uuid4()) for _ in range(2)]
    try:
        await _seed_skip(ids[0], "action_filtered", action="HOLD",
                         symbol="TUNABLES_NVDA")
        await _seed_skip(ids[1], "action_filtered", action="COVER",
                         symbol="TUNABLES_NVDA")

        r = await tunables_simulator(_user={"email": "test@x"}, hours=1)
        actions = {x["action"]: x for x in r["action_what_if"]}
        # HOLD must be filtered out from the suggestion list.
        assert "HOLD" not in actions
        # COVER is a legit action that could be added — surface it.
        assert "COVER" in actions
        assert actions["COVER"]["would_unlock"] == 1
    finally:
        await _reset_state()


async def test_simulator_handles_empty_window():
    """No skips in window → simulator returns empty arrays, not crash."""
    from routes.admin_auto_submit import tunables_simulator
    await _reset_state()
    r = await tunables_simulator(_user={"email": "test@x"}, hours=1)
    assert r["total_skipped"] >= 0  # live preview may have skips
    assert isinstance(r["confidence_what_if"], list)
    assert isinstance(r["lane_what_if"], list)
    assert isinstance(r["action_what_if"], list)
    assert "current_confidence_min" in r


async def test_simulator_skips_above_current_floor():
    """Candidate floors >= current_confidence_min must not appear in
    confidence_what_if — raising the floor never unlocks anything."""
    from routes.admin_auto_submit import tunables_simulator
    await _reset_state()
    iid = str(uuid.uuid4())
    try:
        await _seed_skip(iid, "low_confidence", confidence=0.78)
        r = await tunables_simulator(_user={"email": "test@x"}, hours=1)
        for row in r["confidence_what_if"]:
            assert row["new_min"] < r["current_confidence_min"], (
                f"row {row['new_min']} >= current floor "
                f"{r['current_confidence_min']} — should not be suggested"
            )
    finally:
        await _reset_state()
