"""Pin the post-mortem classifier's handling of the auto_router_*
audit kinds. Regression for the 2026-02-19 prod incident where 2965
intents in the last 24h were being bucketed as "Never submitted (no
audit row)" even though `shared/auto_router.py` had written real
`auto_router_blocked` / `auto_router_advisory_only` rows for them.

If anyone removes the auto_router branches from the classifier the
prod operator dashboard goes back to lying — these tests fail loud.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from db import db
from namespaces import SHARED_INTENTS, SHARED_GATE_RESULTS
from server import app


# A test-only auth bypass — the post-mortem route depends on
# `get_current_user`. We monkeypatch FastAPI's dependency.
from auth import get_current_user


async def _fake_user():
    return {"email": "tester@risedual.io", "role": "admin"}


app.dependency_overrides[get_current_user] = _fake_user


@pytest.fixture
async def clean_collections():
    await db[SHARED_INTENTS].delete_many({"intent_id": {"$regex": "^pm_ar_"}})
    await db[SHARED_GATE_RESULTS].delete_many({"intent_id": {"$regex": "^pm_ar_"}})
    yield
    await db[SHARED_INTENTS].delete_many({"intent_id": {"$regex": "^pm_ar_"}})
    await db[SHARED_GATE_RESULTS].delete_many({"intent_id": {"$regex": "^pm_ar_"}})


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _seed_intent(intent_id: str, lane: str = "equity", brain: str = "camino"):
    await db[SHARED_INTENTS].insert_one({
        "intent_id": intent_id,
        "stack": brain,
        "lane": lane,
        "action": "BUY",
        "symbol": "AAL",
        "ingest_ts": _iso_now(),
        "executed": False,
        "dry_run_state": "passed",
    })


async def _seed_gate_row(intent_id: str, kind: str, **extra):
    doc = {
        "intent_id": intent_id,
        "kind": kind,
        "ts": _iso_now(),
        **extra,
    }
    await db[SHARED_GATE_RESULTS].insert_one(doc)


async def _call_post_mortem():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/api/admin/intents/post-mortem?hours=24")
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.asyncio
async def test_auto_router_blocked_is_classified_as_gate_chain_blocked(clean_collections):
    await _seed_intent("pm_ar_blocked_1")
    await _seed_gate_row(
        "pm_ar_blocked_1",
        kind="auto_router_blocked",
        gates=[{"name": "roadguard_spread_floor", "passed": False}],
    )
    data = await _call_post_mortem()
    assert data["by_outcome"].get("gate_chain_blocked", 0) >= 1
    names = [b["name"] for b in data["top_blockers"]]
    assert "roadguard_spread_floor" in names


@pytest.mark.asyncio
async def test_auto_router_no_trade_is_classified_as_broker_router_blocked(clean_collections):
    await _seed_intent("pm_ar_notrade_1")
    await _seed_gate_row(
        "pm_ar_notrade_1",
        kind="auto_router_no_trade",
        reason="webull_cap_exceeded",
    )
    data = await _call_post_mortem()
    assert data["by_outcome"].get("broker_router_blocked", 0) >= 1
    cats = [b["category"] for b in data["top_blockers"]]
    assert "auto_router_broker" in cats


@pytest.mark.asyncio
async def test_auto_router_error_bucketed_as_submit_error(clean_collections):
    await _seed_intent("pm_ar_err_1")
    await _seed_gate_row(
        "pm_ar_err_1",
        kind="auto_router_error",
        reason="webull_timeout",
    )
    data = await _call_post_mortem()
    assert data["by_outcome"].get("submit_error", 0) >= 1


@pytest.mark.asyncio
async def test_auto_router_passed_counts_as_executed(clean_collections):
    await _seed_intent("pm_ar_pass_1")
    await _seed_gate_row(
        "pm_ar_pass_1",
        kind="auto_router_passed",
    )
    data = await _call_post_mortem()
    assert data["by_outcome"].get("executed", 0) >= 1
    # funnel should reflect at least one submitted+executed event.
    assert data["funnel"]["executed"] >= 1
    assert "pm_ar_pass_1" in data["executed_samples"]


@pytest.mark.asyncio
async def test_auto_router_advisory_only_surfaces_as_filtered_not_stuck(clean_collections):
    await _seed_intent("pm_ar_adv_1")
    await _seed_gate_row(
        "pm_ar_adv_1",
        kind="auto_router_advisory_only",
        classification={"reason": "hold_signal", "executable_candidate": False},
    )
    data = await _call_post_mortem()
    # Outcome key starts with `advisory_only_`
    advisory_keys = [k for k in data["by_outcome"] if k.startswith("advisory_only_")]
    assert advisory_keys, f"expected an advisory_only_* bucket, got {data['by_outcome']}"
