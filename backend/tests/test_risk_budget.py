"""Daily budget reset + cap override on the LIVE risk gate (2026-07-22)."""
from __future__ import annotations

import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, "/app/backend")

from shared.risk.check import _daily_cap_effective, _daily_spent_usd, check


async def _clear(db):
    await db["runtime_flags"].delete_many(
        {"_id": {"$in": ["daily_spend_reset", "risk_caps"]}},
    )
    await db["executions"].delete_many({"symbol": "BUDGT"})


@pytest.mark.asyncio
async def test_cap_override_beats_env_and_reverts():
    from db import db
    await _clear(db)
    try:
        base = await _daily_cap_effective()
        await db["runtime_flags"].update_one(
            {"_id": "risk_caps"}, {"$set": {"cap_daily_usd": 2500.0}}, upsert=True,
        )
        assert await _daily_cap_effective() == 2500.0
        await db["runtime_flags"].update_one(
            {"_id": "risk_caps"}, {"$unset": {"cap_daily_usd": 1}},
        )
        assert await _daily_cap_effective() == base
    finally:
        await _clear(db)


@pytest.mark.asyncio
async def test_reset_marker_zeroes_todays_spend():
    from db import db
    await _clear(db)
    now = datetime.now(timezone.utc)
    try:
        await db["executions"].insert_one({
            "symbol": "BUDGT", "ok": True, "notional_usd": 400.0,
            "ts": now.isoformat(),
        })
        spent = await _daily_spent_usd()
        assert spent >= 400.0

        # Marker after the execution → it no longer counts.
        await db["runtime_flags"].update_one(
            {"_id": "daily_spend_reset"},
            {"$set": {"reset_at": datetime.now(timezone.utc).isoformat()}},
            upsert=True,
        )
        assert await _daily_spent_usd() < spent
    finally:
        await _clear(db)


@pytest.mark.asyncio
async def test_stale_yesterday_marker_is_ignored():
    from db import db
    await _clear(db)
    now = datetime.now(timezone.utc)
    try:
        await db["executions"].insert_one({
            "symbol": "BUDGT", "ok": True, "notional_usd": 123.0,
            "ts": now.isoformat(),
        })
        await db["runtime_flags"].update_one(
            {"_id": "daily_spend_reset"},
            {"$set": {"reset_at": "2020-01-01T00:00:00+00:00"}},
            upsert=True,
        )
        assert await _daily_spent_usd() >= 123.0
    finally:
        await _clear(db)


@pytest.mark.asyncio
async def test_risk_check_unblocks_after_reset(monkeypatch):
    """The exact prod scenario: cap exhausted → RISK reject; RESET
    SPEND → same intent passes."""
    from db import db
    await _clear(db)
    monkeypatch.setenv("RISEDUAL_CAP_DAILY_USD", "50")
    now = datetime.now(timezone.utc)
    intent = {"intent_id": "budget-test-1", "lane": "crypto"}
    try:
        await db["executions"].insert_one({
            "symbol": "BUDGT", "ok": True, "notional_usd": 49.0,
            "ts": now.isoformat(),
        })
        r = await check(intent, notional_usd=5.0)
        assert r.ok is False
        assert r.reason.startswith("daily_cap_exceeded")

        await db["runtime_flags"].update_one(
            {"_id": "daily_spend_reset"},
            {"$set": {"reset_at": datetime.now(timezone.utc).isoformat()}},
            upsert=True,
        )
        r2 = await check(intent, notional_usd=5.0)
        assert r2.ok is True, f"expected pass after reset, got {r2.reason}"
    finally:
        await _clear(db)
