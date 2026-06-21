"""Tests for the 7-stage Intent Funnel endpoint (2026-02-21).

The route aggregates ALL intents in the window, including real
production data. Tests therefore use a uniquely-prefixed brain id
and inspect the `by_brain` bucket for that prefix — leaving the
overall counts unconstrained.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from db import db
from namespaces import SHARED_INTENTS
from shared.pipeline.receipts import PIPELINE_RECEIPTS_COLL
from server import app

from auth import get_current_user


async def _fake_user():
    return {"email": "tester@risedual.io", "role": "admin"}


app.dependency_overrides[get_current_user] = _fake_user


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@pytest.fixture
def brain_id():
    """Unique brain id per test so the by_brain bucket is fully ours."""
    return f"funnelt_{uuid.uuid4().hex[:8]}"


@pytest.fixture
async def cleanup(brain_id):
    yield
    # Best-effort cleanup keyed on the unique brain id (stack field).
    docs = await db[SHARED_INTENTS].find(
        {"stack": brain_id}, {"_id": 0, "intent_id": 1},
    ).to_list(length=1000)
    iids = [d["intent_id"] for d in docs]
    if iids:
        await db[SHARED_INTENTS].delete_many({"intent_id": {"$in": iids}})
        await db[PIPELINE_RECEIPTS_COLL].delete_many({"intent_id": {"$in": iids}})


async def _seed_intent(intent_id: str, brain: str, *, lane: str = "equity",
                       executed: bool = False):
    await db[SHARED_INTENTS].insert_one({
        "intent_id": intent_id,
        "stack": brain,
        "lane": lane,
        "action": "BUY",
        "symbol": "AAL",
        "ingest_ts": _iso_now(),
        "executed": executed,
    })


async def _seed_receipt(intent_id: str, *, final_status: str,
                        restriction_source: str = "broker",
                        broker_called: bool = False):
    await db[PIPELINE_RECEIPTS_COLL].insert_one({
        "intent_id": intent_id,
        "final_status": final_status,
        "restriction_source": restriction_source,
        "broker_called": broker_called,
        "ts": _iso_now(),
    })


async def _get_funnel():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        return await ac.get("/api/admin/intents/funnel?hours=24")


@pytest.mark.asyncio
async def test_funnel_brain_hold_counts_emitted_only(brain_id, cleanup):
    iid = f"ft_{uuid.uuid4().hex[:8]}"
    await _seed_intent(iid, brain_id)
    await _seed_receipt(iid, final_status="NO_ORDER",
                        restriction_source="brain")

    res = await _get_funnel()
    assert res.status_code == 200
    bucket = res.json()["by_brain"][brain_id]
    assert bucket["emitted"] == 1
    assert bucket["seat_approved"] == 0
    assert bucket["governor_sized"] == 0
    assert bucket["filled"] == 0


@pytest.mark.asyncio
async def test_funnel_seat_blocked_counts_emitted_only(brain_id, cleanup):
    iid = f"ft_{uuid.uuid4().hex[:8]}"
    await _seed_intent(iid, brain_id)
    await _seed_receipt(iid, final_status="BLOCKED",
                        restriction_source="seat")

    bucket = (await _get_funnel()).json()["by_brain"][brain_id]
    assert bucket["emitted"] == 1
    assert bucket["seat_approved"] == 0


@pytest.mark.asyncio
async def test_funnel_roadguard_blocked_credits_seat_and_governor(brain_id, cleanup):
    iid = f"ft_{uuid.uuid4().hex[:8]}"
    await _seed_intent(iid, brain_id)
    await _seed_receipt(iid, final_status="BLOCKED",
                        restriction_source="roadguard")

    bucket = (await _get_funnel()).json()["by_brain"][brain_id]
    assert bucket["emitted"] == 1
    assert bucket["seat_approved"] == 1
    assert bucket["governor_sized"] == 1
    assert bucket["roadguard_passed"] == 0


@pytest.mark.asyncio
async def test_funnel_submitted_credits_all_stages_and_filled(brain_id, cleanup):
    iid = f"ft_{uuid.uuid4().hex[:8]}"
    await _seed_intent(iid, brain_id, executed=True)
    await _seed_receipt(iid, final_status="SUBMITTED",
                        restriction_source="broker", broker_called=True)

    bucket = (await _get_funnel()).json()["by_brain"][brain_id]
    for k in ("emitted", "seat_approved", "governor_sized",
              "roadguard_passed", "auto_submit_attempted",
              "broker_accepted", "filled"):
        assert bucket[k] == 1, k


@pytest.mark.asyncio
async def test_funnel_broker_error_counts_attempted_not_accepted(brain_id, cleanup):
    iid = f"ft_{uuid.uuid4().hex[:8]}"
    await _seed_intent(iid, brain_id)
    await _seed_receipt(iid, final_status="BROKER_ERROR",
                        restriction_source="broker", broker_called=True)

    bucket = (await _get_funnel()).json()["by_brain"][brain_id]
    assert bucket["auto_submit_attempted"] == 1
    assert bucket["broker_accepted"] == 0
    assert bucket["filled"] == 0


@pytest.mark.asyncio
async def test_funnel_monotonic_per_brain_bucket(brain_id, cleanup):
    # Mix of outcomes — bucket counts must be non-increasing.
    for i, (status, src, exe) in enumerate([
        ("BLOCKED", "seat", False),
        ("BLOCKED", "roadguard", False),
        ("SUBMITTED", "broker", True),
        ("BROKER_ERROR", "broker", False),
    ]):
        iid = f"ft_{uuid.uuid4().hex[:8]}"
        await _seed_intent(iid, brain_id, executed=exe)
        await _seed_receipt(iid, final_status=status,
                            restriction_source=src,
                            broker_called=status != "BLOCKED")

    bucket = (await _get_funnel()).json()["by_brain"][brain_id]
    keys = ["emitted", "seat_approved", "governor_sized",
            "roadguard_passed", "auto_submit_attempted",
            "broker_accepted", "filled"]
    values = [bucket[k] for k in keys]
    assert all(values[i] >= values[i + 1] for i in range(len(values) - 1)), values


@pytest.mark.asyncio
async def test_funnel_executed_without_receipt_credits_filled(brain_id, cleanup):
    """Legacy path: executed=True but no pipeline_receipt → still
    counts as Filled (ground truth)."""
    iid = f"ft_{uuid.uuid4().hex[:8]}"
    await _seed_intent(iid, brain_id, executed=True)
    # no receipt seeded.

    bucket = (await _get_funnel()).json()["by_brain"][brain_id]
    assert bucket["emitted"] == 1
    assert bucket["filled"] == 1


@pytest.mark.asyncio
async def test_funnel_shape_has_seven_stages():
    res = await _get_funnel()
    assert res.status_code == 200
    body = res.json()
    keys = [s["key"] for s in body["stages"]]
    assert keys == [
        "emitted", "seat_approved", "governor_sized", "roadguard_passed",
        "auto_submit_attempted", "broker_accepted", "filled",
    ]
    # biggest_drop is either None (no intents) or has the contract shape.
    bd = body["biggest_drop"]
    if bd is not None:
        assert {"from", "to", "lost", "drop_pct"} <= set(bd.keys())


# ── Stage-shift detection tests ─────────────────────────────────────
from datetime import timedelta
from routes.admin_intents_funnel import FUNNEL_SNAPSHOTS_COLL


@pytest.fixture
async def clean_snapshots():
    """Wipe stage-shift snapshots so shift detection is deterministic."""
    await db[FUNNEL_SNAPSHOTS_COLL].delete_many({})
    yield
    await db[FUNNEL_SNAPSHOTS_COLL].delete_many({})


@pytest.mark.asyncio
async def test_funnel_stage_shift_none_on_first_call(clean_snapshots):
    """No prior snapshot → stage_shift is None."""
    body = (await _get_funnel()).json()
    assert body["stage_shift"] is None


@pytest.mark.asyncio
async def test_funnel_stage_shift_none_when_stage_unchanged(clean_snapshots):
    """Two consecutive calls with the same biggest-drop → no shift."""
    await _get_funnel()
    body = (await _get_funnel()).json()
    # The data hasn't changed; biggest_drop.to should be identical.
    assert body["stage_shift"] is None


@pytest.mark.asyncio
async def test_funnel_stage_shift_fires_when_biggest_drop_moves(
    brain_id, cleanup, clean_snapshots,
):
    """Plant a snapshot pointing at a different stage > 60s ago,
    then call the endpoint and expect a stage_shift payload."""
    # Seed at least one intent so biggest_drop is well-defined.
    iid = f"ft_{uuid.uuid4().hex[:8]}"
    await _seed_intent(iid, brain_id)
    await _seed_receipt(iid, final_status="BLOCKED",
                        restriction_source="seat")
    # First, take a real snapshot so we have one in the collection.
    body0 = (await _get_funnel()).json()
    real_to = body0["biggest_drop"]["to"]

    # Backdate the snapshot and pretend the prior leak was elsewhere.
    different_stage = "Broker accepted" if real_to != "Broker accepted" \
        else "Filled"
    await db[FUNNEL_SNAPSHOTS_COLL].update_one(
        {},
        {"$set": {
            "biggest_drop_to": different_stage,
            "captured_at": datetime.now(timezone.utc) - timedelta(seconds=120),
        }},
        upsert=False,
    )
    # Wipe any other snapshots so this is unambiguously the latest.
    keep_id = (await db[FUNNEL_SNAPSHOTS_COLL].find_one({}))["_id"]
    await db[FUNNEL_SNAPSHOTS_COLL].delete_many({"_id": {"$ne": keep_id}})

    body = (await _get_funnel()).json()
    shift = body["stage_shift"]
    assert shift is not None, "Expected stage_shift to fire"
    assert shift["from_stage"] == different_stage
    assert shift["to_stage"] == real_to
    assert shift["gap_seconds"] >= 60
