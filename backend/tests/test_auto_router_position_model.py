"""Tests for the auto-router position-model alignment (2026-05-31).

Doctrine pin: the auto-router's pickup filter and the seat-mismatch
sweep MUST ask the same question the gate chain asks:
  "Does ANY brain currently hold the executor seat for this lane?"

NOT the old brain-coupled question:
  "Did the brain that POSTED this intent hold the seat at post-time?"

The latter caused REDEYE's crypto BUYs (posted while Alpha held the
executor seat) to be silently terminated by the auto-router's 30-second
sweep tick, even though the gate chain — post-position-model fix —
would have passed them.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from db import db
from namespaces import SHARED_GATE_RESULTS, SHARED_INTENTS
from shared.auto_router import _sweep_seat_mismatched_intents


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _pending_intent(
    *, intent_id: str, stack: str, lane: str, posted_by_seat_holder: bool = False,
) -> dict:
    return {
        "intent_id": intent_id,
        "stack": stack,
        "symbol": "AAPL" if lane == "equity" else "BTC/USD",
        "action": "BUY",
        "lane": lane,
        "may_execute": False,
        "requires_gate_pass": True,
        "gate_state": "pending",
        "executed": False,
        "holds_executor_seat": posted_by_seat_holder,
        "executor_holder_at_post": "alpha" if not posted_by_seat_holder else stack,
        "created_at": _now_iso(),
        "confidence": 0.7,
    }


@pytest.fixture
async def cleanup_intents():
    """Yields a list-collector; each id added gets cleaned up after."""
    ids: list[str] = []
    yield ids
    if ids:
        await db[SHARED_INTENTS].delete_many({"intent_id": {"$in": ids}})
        await db[SHARED_GATE_RESULTS].delete_many({"intent_id": {"$in": ids}})


@pytest.mark.asyncio
async def test_sweep_leaves_cross_brain_intent_alone_when_lane_has_holder(cleanup_intents):
    """REDEYE posts a crypto BUY while Alpha holds the crypto executor
    seat. Under the OLD brain-coupled sweep, this would have been
    marked `gate_state=blocked` instantly. Under the position-model
    sweep, it must be LEFT pending so the gate chain (which is now
    position-model) can pass it on the next auto-router tick."""
    iid = f"swp-{uuid.uuid4().hex[:10]}"
    cleanup_intents.append(iid)
    await db[SHARED_INTENTS].insert_one(
        _pending_intent(intent_id=iid, stack="redeye", lane="crypto",
                        posted_by_seat_holder=False),
    )

    # Stub seat: alpha currently holds the crypto executor seat.
    with patch(
        "shared.executor_seat.get_seat_holder",
        new=AsyncMock(side_effect=lambda seat: "alpha" if "crypto" in seat else None),
    ), patch(
        "shared.executor_seat.seats_with_execute",
        return_value=["crypto_executor"],
    ):
        await _sweep_seat_mismatched_intents()

    # Sweep took no action on this intent — current holder exists.
    refreshed = await db[SHARED_INTENTS].find_one(
        {"intent_id": iid}, {"_id": 0, "gate_state": 1},
    )
    assert refreshed["gate_state"] == "pending", (
        "OLD doctrine bug: sweep killed an intent that the position-model gate would pass"
    )


@pytest.mark.asyncio
async def test_sweep_terminally_blocks_intent_when_lane_has_no_holder(cleanup_intents):
    """If NO brain currently holds the executor seat for the lane,
    the intent has no path forward — the gate chain would fail it too.
    The sweep terminally-blocks it with a typed reason so the operator
    queue tells the truth."""
    iid = f"swp-{uuid.uuid4().hex[:10]}"
    cleanup_intents.append(iid)
    await db[SHARED_INTENTS].insert_one(
        _pending_intent(intent_id=iid, stack="redeye", lane="crypto",
                        posted_by_seat_holder=False),
    )

    # Seat vacant in every eligible execute-seat for the lane.
    with patch(
        "shared.executor_seat.get_seat_holder",
        new=AsyncMock(return_value=None),
    ), patch(
        "shared.executor_seat.seats_with_execute",
        return_value=["crypto_executor"],
    ):
        await _sweep_seat_mismatched_intents()

    refreshed = await db[SHARED_INTENTS].find_one(
        {"intent_id": iid}, {"_id": 0, "gate_state": 1},
    )
    assert refreshed["gate_state"] == "blocked"


@pytest.mark.asyncio
async def test_sweep_block_reason_names_the_doctrine_change(cleanup_intents):
    """The new sweep's block reason must NOT use the old brain-coupled
    phrasing ('intent posted when seat held by X, not Y'). It must
    name the lane and indicate the lane has no holder, so audits show
    the new doctrine."""
    iid = f"swp-{uuid.uuid4().hex[:10]}"
    cleanup_intents.append(iid)
    await db[SHARED_INTENTS].insert_one(
        _pending_intent(intent_id=iid, stack="redeye", lane="crypto"),
    )
    with patch(
        "shared.executor_seat.get_seat_holder",
        new=AsyncMock(return_value=None),
    ), patch(
        "shared.executor_seat.seats_with_execute",
        return_value=["crypto_executor"],
    ):
        await _sweep_seat_mismatched_intents()

    gr = await db[SHARED_GATE_RESULTS].find_one(
        {"intent_id": iid}, {"_id": 0}, sort=[("ts", -1)],
    )
    assert gr is not None
    reason = (gr["gates"][0] or {}).get("reason", "")
    assert "no current executor-seat holder" in reason
    assert "crypto" in reason


@pytest.mark.asyncio
async def test_sweep_does_not_re_block_already_executed_intents(cleanup_intents):
    """A successfully-executed intent must NEVER be re-swept. Belt and
    braces: the sweep query already filters `executed: {$ne: True}`,
    but if a race ever flipped one, the sweep MUST NOT undo it."""
    iid = f"swp-{uuid.uuid4().hex[:10]}"
    cleanup_intents.append(iid)
    doc = _pending_intent(intent_id=iid, stack="redeye", lane="crypto")
    doc["executed"] = True
    doc["gate_state"] = "passed"
    await db[SHARED_INTENTS].insert_one(doc)

    with patch(
        "shared.executor_seat.get_seat_holder",
        new=AsyncMock(return_value=None),
    ), patch(
        "shared.executor_seat.seats_with_execute",
        return_value=["crypto_executor"],
    ):
        await _sweep_seat_mismatched_intents()

    refreshed = await db[SHARED_INTENTS].find_one(
        {"intent_id": iid}, {"_id": 0, "gate_state": 1, "executed": 1},
    )
    assert refreshed["executed"] is True
    assert refreshed["gate_state"] == "passed"
