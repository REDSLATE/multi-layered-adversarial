"""Tripwires for limbo cleanup + per-intent inspection.

Doctrine pin (2026-02-18):
    The auto-router USED to pick up only `holds_executor_seat=True`
    intents. Anything posted when the brain didn't hold the seat
    silently accumulated as `gate_state=pending` forever (the
    "May 18 trades in limbo" symptom). The fix: a sweep that flips
    those to `gate_state=blocked` with a typed reason, plus a
    per-intent inspection endpoint so the operator can read out
    exactly why any specific intent is in whatever state it's in.
"""
from __future__ import annotations

import pytest


# ─── Seat-mismatch sweep ──────────────────────────────────────────────


@pytest.mark.tripwire
async def test_sweep_flips_seat_mismatched_pending_to_blocked():
    """An intent posted with `holds_executor_seat=False` and
    `gate_state=pending` MUST be terminally disposed by the sweep."""
    from db import db
    from namespaces import SHARED_INTENTS
    from shared.auto_router import _sweep_seat_mismatched_intents

    await db[SHARED_INTENTS].insert_one({
        "intent_id": "tw-limbo-1",
        "stack": "camaro", "symbol": "BTC/USD", "action": "BUY",
        "lane": "crypto",
        "gate_state": "pending",
        "executed": False,
        "holds_executor_seat": False,
        "executor_holder_at_post": "alpha",
        "ingest_ts": "2026-05-18T12:00:00+00:00",
    })

    swept = await _sweep_seat_mismatched_intents()
    assert swept >= 1

    after = await db[SHARED_INTENTS].find_one(
        {"intent_id": "tw-limbo-1"}, {"_id": 0, "gate_state": 1},
    )
    assert after["gate_state"] == "blocked"
    await db[SHARED_INTENTS].delete_one({"intent_id": "tw-limbo-1"})


@pytest.mark.tripwire
async def test_sweep_does_not_touch_seat_holding_pending():
    """A `pending` intent that DOES hold the seat MUST be left
    alone — the auto-router's main loop processes those."""
    from db import db
    from namespaces import SHARED_INTENTS
    from shared.auto_router import _sweep_seat_mismatched_intents

    await db[SHARED_INTENTS].insert_one({
        "intent_id": "tw-keepme-1",
        "stack": "camaro", "symbol": "BTC/USD", "action": "BUY",
        "lane": "crypto",
        "gate_state": "pending",
        "executed": False,
        "holds_executor_seat": True,
        "executor_holder_at_post": "camaro",
        "ingest_ts": "2026-05-22T16:00:00+00:00",
    })
    await _sweep_seat_mismatched_intents()
    after = await db[SHARED_INTENTS].find_one(
        {"intent_id": "tw-keepme-1"}, {"_id": 0, "gate_state": 1},
    )
    assert after["gate_state"] == "pending", (
        "sweep MUST NOT touch seat-holding intents — they're the "
        "auto-router main loop's responsibility"
    )
    await db[SHARED_INTENTS].delete_one({"intent_id": "tw-keepme-1"})


# ─── Per-intent inspection ────────────────────────────────────────────


@pytest.mark.tripwire
def test_inspect_route_requires_auth(base_url):
    import requests
    r = requests.get(f"{base_url}/api/admin/intent/anything/inspect", timeout=15)
    assert r.status_code in (401, 403)


@pytest.mark.tripwire
def test_inspect_unknown_intent_returns_404(auth_client, base_url):
    r = auth_client.get(
        f"{base_url}/api/admin/intent/nonexistent-id/inspect", timeout=15,
    )
    assert r.status_code == 404


@pytest.mark.tripwire
async def test_inspect_returns_terminal_vs_transient_hint(auth_client, base_url):
    """The inspection MUST classify each failed gate as terminal or
    transient so the operator knows whether to expect MC to retry."""
    from db import db
    from namespaces import SHARED_INTENTS

    # 2026-02-19 doctrine update: executor_seat_check now uses the
    # POSITION model — authority lives in the seat, not the
    # holder-at-post-time — so a seat-mismatched intent no longer
    # constitutes a terminal failure (the gate passes when ANY brain
    # currently holds the seat for the lane). To still pin "terminal"
    # failure_kind, we plant an intent that violates the
    # schema_invariants gate (`may_execute` pinned True breaks the
    # invariant), which is intent-frozen and so the failure must
    # come back classified terminal by the inspect endpoint.
    # Use a unique intent_id so no background worker that touched a
    # previous fixture row mutates this one.
    import uuid as _uuid
    intent_id = f"tw-inspect-{_uuid.uuid4().hex[:10]}"
    await db[SHARED_INTENTS].insert_one({
        "intent_id": intent_id,
        "stack": "camaro", "symbol": "AAPL", "action": "BUY",
        "lane": "equity",
        "confidence": 0.7,
        "may_execute": True,         # violates schema_invariants
        "requires_gate_pass": True,
        "gate_state": "pending",
        "executed": False,
        "holds_executor_seat": False,
        "executor_holder_at_post": "alpha",
        "snapshot": {"spread_bps": 5.0},
        "ingest_ts": "2026-05-18T12:00:00+00:00",
    })
    r = auth_client.get(
        f"{base_url}/api/admin/intent/{intent_id}/inspect", timeout=15,
    )
    assert r.status_code == 200
    body = r.json()
    schema_gate = next(
        g for g in body["live_gate_chain"]
        if g["name"] == "schema_invariants"
    )
    assert schema_gate["passed"] is False
    assert schema_gate["failure_kind"] == "terminal", (
        f"schema_invariants failure MUST be classified terminal "
        f"(may_execute pinning is frozen on the intent); got {schema_gate!r}"
    )
    # Summary line must mention the gate name.
    assert "schema_invariants" in body["summary"]
    await db[SHARED_INTENTS].delete_one({"intent_id": intent_id})


# ─── Operator dispose ────────────────────────────────────────────────


@pytest.mark.tripwire
async def test_operator_dispose_flips_pending_to_blocked(auth_client, base_url):
    from db import db
    from namespaces import SHARED_INTENTS

    await db[SHARED_INTENTS].insert_one({
        "intent_id": "tw-dispose-1",
        "stack": "camaro", "symbol": "AAPL", "action": "BUY",
        "lane": "equity",
        "gate_state": "pending",
        "executed": False,
        "holds_executor_seat": False,
    })
    r = auth_client.post(
        f"{base_url}/api/admin/intent/tw-dispose-1/dispose", timeout=15,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["previous"] == "pending"
    assert body["current"] == "blocked"

    after = await db[SHARED_INTENTS].find_one(
        {"intent_id": "tw-dispose-1"}, {"_id": 0},
    )
    assert after["gate_state"] == "blocked"
    assert after["operator_disposed"] is True
    await db[SHARED_INTENTS].delete_one({"intent_id": "tw-dispose-1"})


@pytest.mark.tripwire
def test_operator_dispose_rejects_non_pending(auth_client, base_url):
    import asyncio
    from db import db
    from namespaces import SHARED_INTENTS

    async def _setup():
        await db[SHARED_INTENTS].insert_one({
            "intent_id": "tw-dispose-2",
            "stack": "camaro", "symbol": "AAPL", "action": "BUY",
            "lane": "equity",
            "gate_state": "blocked",  # already disposed
            "executed": False,
        })
    asyncio.get_event_loop().run_until_complete(_setup())

    r = auth_client.post(
        f"{base_url}/api/admin/intent/tw-dispose-2/dispose", timeout=15,
    )
    assert r.status_code == 400

    async def _cleanup():
        await db[SHARED_INTENTS].delete_one({"intent_id": "tw-dispose-2"})
    asyncio.get_event_loop().run_until_complete(_cleanup())
