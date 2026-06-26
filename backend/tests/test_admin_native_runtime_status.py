"""Unit tests for the native-runtime status endpoint logic.

We test `_brain_status` directly rather than going through the
HTTP route — the cookie/JWT auth dance is exercised by other tests
and not what's interesting here. What IS interesting:

  1. Brain reports `silent=True` when `enabled=True` AND no recent tick
  2. Brain reports `silent=False` when disabled (regardless of tick age)
  3. Tick aggregation (count_60m, emitted_60m, errors_60m) is correct
"""
from __future__ import annotations

import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, "/app/backend")

from routes.admin_native_runtime_status import (  # noqa: E402
    _brain_status, SILENT_THRESHOLD_SEC,
)
from shared.runtime import barracuda_runtime  # noqa: E402


@pytest.mark.asyncio
async def test_brain_status_disabled_is_never_silent(monkeypatch):
    monkeypatch.delenv("BARRACUDA_NATIVE_RUNTIME_ENABLED", raising=False)
    from db import db

    marker = f"st-{uuid.uuid4().hex[:6]}"
    coll = f"_test_status_ticks_{marker}"
    try:
        # Ancient tick — should NOT trigger silent because brain is off.
        ancient = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        await db[coll].insert_one({
            "brain_id": "barracuda",
            "started_at": ancient,
            "emitted_count": 0,
            "error_count": 0,
        })
        row = await _brain_status("barracuda", barracuda_runtime, coll)
        assert row["enabled"] is False
        assert row["silent"] is False
        assert row["tick_age_sec"] is not None
        assert row["tick_age_sec"] > SILENT_THRESHOLD_SEC
    finally:
        await db[coll].drop()


@pytest.mark.asyncio
async def test_brain_status_silent_when_enabled_and_no_recent_tick(monkeypatch):
    monkeypatch.setenv("BARRACUDA_NATIVE_RUNTIME_ENABLED", "true")
    from db import db

    marker = f"st-{uuid.uuid4().hex[:6]}"
    coll = f"_test_status_ticks_{marker}"
    try:
        ancient = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        await db[coll].insert_one({
            "brain_id": "barracuda",
            "started_at": ancient,
            "emitted_count": 0,
            "error_count": 0,
        })
        row = await _brain_status("barracuda", barracuda_runtime, coll)
        assert row["enabled"] is True
        assert row["silent"] is True
        assert row["tick_age_sec"] > SILENT_THRESHOLD_SEC
    finally:
        await db[coll].drop()


@pytest.mark.asyncio
async def test_brain_status_aggregates_last_60m(monkeypatch):
    monkeypatch.delenv("BARRACUDA_NATIVE_RUNTIME_ENABLED", raising=False)
    from db import db

    marker = f"st-{uuid.uuid4().hex[:6]}"
    coll = f"_test_status_ticks_{marker}"
    try:
        now = datetime.now(timezone.utc)
        # 3 recent ticks (last 30 min)
        for i, (em, er) in enumerate(((5, 0), (2, 1), (4, 0))):
            await db[coll].insert_one({
                "brain_id": "barracuda",
                "started_at": (now - timedelta(minutes=10 * (i + 1))).isoformat(),
                "emitted_count": em,
                "error_count": er,
            })
        # 1 ancient tick that should NOT be counted in 60m window
        await db[coll].insert_one({
            "brain_id": "barracuda",
            "started_at": (now - timedelta(hours=3)).isoformat(),
            "emitted_count": 99,
            "error_count": 99,
        })
        row = await _brain_status("barracuda", barracuda_runtime, coll)
        assert row["tick_count_60m"] == 3
        assert row["emitted_60m"] == 11
        assert row["errors_60m"] == 1
    finally:
        await db[coll].drop()


def test_silent_threshold_is_five_minutes():
    # Doctrine: 5 min is the operator-stated "ticker should fire at
    # 60s; missing 5 ticks in a row is the silent-worker symptom".
    assert SILENT_THRESHOLD_SEC == 5 * 60
