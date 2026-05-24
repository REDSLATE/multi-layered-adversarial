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

    # Plant an intent that fails terminally on `executor_seat_check`
    # (it didn't hold the seat at post-time). Schema-pin fields are
    # required by gate 1 (`schema_invariants`) — without them the
    # intent dies at gate 1 and we never reach gate 3 (2026-05-17
    # schema tightening; fixture updated 2026-05-24).
    await db[SHARED_INTENTS].insert_one({
        "intent_id": "tw-inspect-1",
        "stack": "camaro", "symbol": "AAPL", "action": "BUY",
        "lane": "equity",
        "confidence": 0.7,
        "may_execute": False,
        "requires_gate_pass": True,
        "gate_state": "pending",
        "executed": False,
        "holds_executor_seat": False,
        "executor_holder_at_post": "alpha",
        "snapshot": {"spread_bps": 5.0},
        "ingest_ts": "2026-05-18T12:00:00+00:00",
    })
    r = auth_client.get(
        f"{base_url}/api/admin/intent/tw-inspect-1/inspect", timeout=15,
    )
    assert r.status_code == 200
    body = r.json()
    seat_gate = next(
        g for g in body["live_gate_chain"]
        if g["name"] == "executor_seat_check"
    )
    assert seat_gate["passed"] is False
    assert seat_gate["failure_kind"] == "terminal", (
        f"executor_seat_check failure MUST be classified terminal "
        f"(holder_at_post is frozen on the intent); got {seat_gate!r}"
    )
    # Summary line must mention the gate name.
    assert "executor_seat_check" in body["summary"]
    await db[SHARED_INTENTS].delete_one({"intent_id": "tw-inspect-1"})


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
