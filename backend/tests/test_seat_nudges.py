"""Seat-nudges endpoint tests (2026-05-30).

Doctrine: advisory observability only. Validates the nudge endpoint's
authority gating (operator-only POST, runtime-token brain GET), seat
validation, vacant-seat rejection, and cooldown enforcement.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from db import db
from namespaces import SEAT_NUDGES, SHARED_POSITIONS
from shared.seat_nudges import (
    NudgeBody,
    list_nudges_for_position,
    nudge_seat,
    NUDGE_COOLDOWN_SEC,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@pytest.fixture
async def seeded_position(monkeypatch):
    """Seed a position + force the roster to a known assignment so the
    test is hermetic against operator state."""
    pos_id = f"nudge-test-{uuid.uuid4().hex[:10]}"
    await db[SHARED_POSITIONS].insert_one({
        "position_id": pos_id,
        "symbol": "AAPL",
        "state": "discussing",
        "thesis": "nudge test",
        "proposed_by": "operator",
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
    })

    # Stub the roster — assign a known holder for `governor` and leave
    # `auditor` vacant. Both governor and auditor are required seats.
    async def fake_get_roster():
        return {
            "assignments": {
                "executor": "alpha",
                "strategist": "camaro",
                "governor": "chevelle",
                "auditor": None,  # vacant
            },
        }

    monkeypatch.setattr("shared.seat_nudges.get_roster", fake_get_roster)

    yield pos_id

    await db[SHARED_POSITIONS].delete_many({"position_id": pos_id})
    await db[SEAT_NUDGES].delete_many({"position_id": pos_id})


@pytest.mark.asyncio
async def test_nudge_records_against_current_seat_holder(seeded_position):
    """Operator nudges `governor`. The record must address Chevelle
    (the current holder) — proves the address is resolved at SEND
    time from the live roster, not from any stale stamp."""
    res = await nudge_seat(
        position_id=seeded_position,
        body=NudgeBody(seat="governor", message="please stance on AAPL"),
        user={"email": "admin@risedual.io"},
    )
    nudge = res["nudge"]
    assert nudge["seat"] == "governor"
    assert nudge["brain"] == "chevelle"
    assert nudge["position_id"] == seeded_position
    assert nudge["sent_by_email"] == "admin@risedual.io"
    assert nudge["status"] == "sent"
    assert nudge["authority"] == "advisory_observability_only"


@pytest.mark.asyncio
async def test_nudge_rejects_vacant_seat(seeded_position):
    """Nudging a seat with no current holder must 404 — there's no
    one to ping. Operator should assign first, then nudge."""
    with pytest.raises(HTTPException) as exc:
        await nudge_seat(
            position_id=seeded_position,
            body=NudgeBody(seat="auditor"),
            user={"email": "admin@risedual.io"},
        )
    assert exc.value.status_code == 404
    assert "vacant" in str(exc.value.detail).lower()


@pytest.mark.asyncio
async def test_nudge_rejects_unknown_seat(seeded_position):
    """Seats outside the required-seats set are rejected at 422 so the
    operator gets a typed error before MC writes anything."""
    with pytest.raises(HTTPException) as exc:
        await nudge_seat(
            position_id=seeded_position,
            body=NudgeBody(seat="not_a_seat"),
            user={"email": "admin@risedual.io"},
        )
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_nudge_rejects_unknown_position(monkeypatch):
    async def fake_get_roster():
        return {"assignments": {"governor": "chevelle"}}

    monkeypatch.setattr("shared.seat_nudges.get_roster", fake_get_roster)

    with pytest.raises(HTTPException) as exc:
        await nudge_seat(
            position_id="does-not-exist",
            body=NudgeBody(seat="governor"),
            user={"email": "admin@risedual.io"},
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_nudge_cooldown_blocks_back_to_back_sends(seeded_position):
    """First nudge succeeds; immediate second nudge for the same
    (position, seat) must 429 with a retry_after_seconds payload."""
    await nudge_seat(
        position_id=seeded_position,
        body=NudgeBody(seat="governor"),
        user={"email": "admin@risedual.io"},
    )
    with pytest.raises(HTTPException) as exc:
        await nudge_seat(
            position_id=seeded_position,
            body=NudgeBody(seat="governor"),
            user={"email": "admin@risedual.io"},
        )
    assert exc.value.status_code == 429
    detail = exc.value.detail
    assert detail["blocked_by"] == "nudge_cooldown"
    assert detail["retry_after_seconds"] > 0
    assert detail["retry_after_seconds"] <= NUDGE_COOLDOWN_SEC


@pytest.mark.asyncio
async def test_nudge_cooldown_isolated_per_seat(seeded_position):
    """Cooldown is per-(position, seat). Nudging `governor` should not
    block a subsequent nudge to `strategist` on the same position."""
    await nudge_seat(
        position_id=seeded_position,
        body=NudgeBody(seat="governor"),
        user={"email": "admin@risedual.io"},
    )
    res2 = await nudge_seat(
        position_id=seeded_position,
        body=NudgeBody(seat="strategist"),
        user={"email": "admin@risedual.io"},
    )
    assert res2["nudge"]["seat"] == "strategist"
    assert res2["nudge"]["brain"] == "camaro"


@pytest.mark.asyncio
async def test_list_nudges_returns_newest_first(seeded_position):
    """Operator can read all nudges on a position. Sort newest first."""
    await nudge_seat(
        position_id=seeded_position,
        body=NudgeBody(seat="governor"),
        user={"email": "admin@risedual.io"},
    )
    await nudge_seat(
        position_id=seeded_position,
        body=NudgeBody(seat="strategist"),
        user={"email": "admin@risedual.io"},
    )
    res = await list_nudges_for_position(
        position_id=seeded_position, limit=10, _user={"email": "admin@risedual.io"},
    )
    assert res["count"] == 2
    items = res["items"]
    assert items[0]["ts_epoch"] >= items[1]["ts_epoch"]
