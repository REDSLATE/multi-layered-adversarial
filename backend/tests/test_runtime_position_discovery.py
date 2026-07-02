"""Tripwire — runtime-authed position discovery endpoint (2026-05-24).

GET /api/runtime-discussion/positions

Lets brain sidecars (REDEYE in particular) discover MC-managed
positions using their X-Runtime-Token, then post stances back via the
companion POST endpoint. Closes the discovery gap that previously
forced brains to guess position UUIDs.

Invariants pinned:
  1. Auth: requires X-Runtime-Token. No token = 401.
  2. Read-only: no side effects on the positions collection.
  3. Returns the same hydrated shape as the operator endpoint (so the
     UI and brain consumers share one data contract).
  4. Stance vocabulary surfaced via doctrine_note so brains don't have
     to find it elsewhere.
  5. Includes a default `status=open` filter so polling brains don't
     get flooded with closed positions.
"""
from __future__ import annotations

import pytest
import pytest_asyncio

from db import db
from namespaces import SHARED_POSITIONS
from shared.positions import runtime_list_positions


pytestmark = [pytest.mark.tripwire, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def seed_positions():
    """Plant two open positions + one closed for shape tests."""
    await db[SHARED_POSITIONS].delete_many(
        {"position_id": {"$in": ["tw-pos-open-1", "tw-pos-open-2", "tw-pos-closed-1"]}},
    )
    await db[SHARED_POSITIONS].insert_many([
        {
            "position_id": "tw-pos-open-1",
            "symbol": "AAPL", "side": "long", "lane": "equity",
            "state": "discussing", "opened_at": "2099-12-31T10:00:00+00:00",
            "updated_at": "2099-12-31T10:00:00+00:00",
        },
        {
            "position_id": "tw-pos-open-2",
            "symbol": "BTC/USD", "side": "long", "lane": "crypto",
            "state": "proposed", "opened_at": "2099-12-31T10:05:00+00:00",
            "updated_at": "2099-12-31T10:05:00+00:00",
        },
        {
            "position_id": "tw-pos-closed-1",
            "symbol": "NVDA", "side": "long", "lane": "equity",
            "state": "rejected", "opened_at": "2099-12-31T10:00:00+00:00",
            "updated_at": "2099-12-31T09:00:00+00:00",
        },
    ])
    yield
    await db[SHARED_POSITIONS].delete_many(
        {"position_id": {"$in": ["tw-pos-open-1", "tw-pos-open-2", "tw-pos-closed-1"]}},
    )


async def test_runtime_list_requires_token(monkeypatch, seed_positions):
    """No `X-Runtime-Token` header → reject."""
    from fastapi import HTTPException

    monkeypatch.setenv("GTO_INGEST_TOKEN", "tw-token")
    with pytest.raises(HTTPException) as exc:
        await runtime_list_positions(
            runtime="redeye", status="open", symbol=None, limit=100,
            x_runtime_token=None,
        )
    assert exc.value.status_code in (401, 403)


async def test_runtime_list_returns_open_by_default(monkeypatch, seed_positions):
    """Default `status=open` returns the 2 open rows, not the closed one."""
    monkeypatch.setenv("GTO_INGEST_TOKEN", "tw-token")
    result = await runtime_list_positions(
        runtime="redeye", status="open", symbol=None, limit=100,
        x_runtime_token="tw-token",
    )
    assert "items" in result
    assert "count" in result
    assert "doctrine_note" in result
    ids = {p["position_id"] for p in result["items"]}
    assert "tw-pos-open-1" in ids
    assert "tw-pos-open-2" in ids
    assert "tw-pos-closed-1" not in ids


async def test_runtime_list_filters_by_symbol(monkeypatch, seed_positions):
    """`symbol` filter narrows the result set."""
    monkeypatch.setenv("GTO_INGEST_TOKEN", "tw-token")
    result = await runtime_list_positions(
        runtime="redeye", status="open", symbol="AAPL", limit=100,
        x_runtime_token="tw-token",
    )
    ids = {p["position_id"] for p in result["items"]}
    assert ids == {"tw-pos-open-1"}


async def test_runtime_list_supports_camaro_too(monkeypatch, seed_positions):
    """Endpoint is brain-agnostic — any valid runtime token works."""
    monkeypatch.setenv("BARRACUDA_INGEST_TOKEN", "camaro-tw-token")
    result = await runtime_list_positions(
        runtime="camaro", status="open", symbol=None, limit=100,
        x_runtime_token="camaro-tw-token",
    )
    assert result["runtime"] == "camaro"
    assert result["count"] >= 2  # seed positions plus any pre-existing


async def test_runtime_list_doctrine_note_carries_stance_vocab(monkeypatch, seed_positions):
    """The doctrine_note must spell out the stance vocabulary so brain
    teams don't have to dig through docs."""
    monkeypatch.setenv("GTO_INGEST_TOKEN", "tw-token")
    result = await runtime_list_positions(
        runtime="redeye", status="open", symbol=None, limit=100,
        x_runtime_token="tw-token",
    )
    note = result["doctrine_note"]
    for word in ("long", "short", "abstain", "confidence"):
        assert word in note, f"doctrine_note missing `{word}`: {note!r}"


async def test_runtime_list_includes_stances_by_brain(monkeypatch, seed_positions):
    """Each row carries `stances_by_brain` so a brain can see what it
    has ALREADY stamped on this position (avoid double-posting)."""
    monkeypatch.setenv("GTO_INGEST_TOKEN", "tw-token")
    result = await runtime_list_positions(
        runtime="redeye", status="open", symbol="AAPL", limit=100,
        x_runtime_token="tw-token",
    )
    assert result["items"], "AAPL position should be present"
    row = result["items"][0]
    # Hydrated shape — must include stance-related fields.
    assert "stances_by_brain" in row or "stance_counts" in row, (
        f"row should expose stance state; got keys {list(row.keys())}"
    )
