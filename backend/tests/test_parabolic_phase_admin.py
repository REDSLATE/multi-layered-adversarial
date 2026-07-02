"""Tests for the graceful-degrade contract on the Parabolic Phase Map
endpoint. Doctrine (2026-07-03): the dashboard NEVER surfaces a raw
Atlas exception. Any Mongo failure must be caught and returned as HTTP
200 with an `error` field the strip renders as a soft paused-tape
banner rather than a red exception panel.

Regression guard for the fix that mirrors the mc_shelly pattern.
"""
from __future__ import annotations

import asyncio
import sys

import pytest

sys.path.insert(0, "/app")
sys.path.insert(0, "/app/backend")


class _FakeAggregate:
    def __init__(self, behavior: str, rows=None):
        self._behavior = behavior
        self._rows = rows or []

    async def to_list(self, _length):
        if self._behavior == "timeout":
            # Sleep longer than the endpoint's 8s wait_for so the
            # wrapper trips a TimeoutError deterministically.
            await asyncio.sleep(30)
        if self._behavior == "raise":
            raise RuntimeError("NetworkTimeout: Atlas read failed")
        return list(self._rows)


class _FakeCollection:
    def __init__(self, behavior: str, rows=None):
        self._behavior = behavior
        self._rows = rows

    def aggregate(self, _pipeline):
        return _FakeAggregate(self._behavior, self._rows)


class _FakeDB:
    def __init__(self, behavior: str, rows=None):
        self._coll = _FakeCollection(behavior, rows)

    def __getitem__(self, _name):
        return self._coll


@pytest.mark.asyncio
async def test_endpoint_returns_soft_error_on_atlas_timeout(monkeypatch):
    from backend.routes import parabolic_phase_admin

    # Shorten the timeout so the test finishes in under 1s instead of 8s
    monkeypatch.setattr(parabolic_phase_admin, "_MONGO_READ_TIMEOUT_S", 0.1)
    monkeypatch.setattr(
        parabolic_phase_admin, "db",
        _FakeDB("timeout"),
    )

    result = await parabolic_phase_admin.get_phase_counts(_user={})
    assert result["error"] == "mongo_timeout"
    # All counts must be zeroed — soft-degrade contract.
    assert result["total_classified"] == 0
    assert result["counts"]["accumulation"] == 0
    assert result["counts"]["parabolic"] == 0
    # Symbols dict shape preserved so the strip doesn't crash rendering.
    assert set(result["symbols"].keys()) == set(result["counts"].keys())
    # Human-readable message present (the strip renders this).
    assert "timed out" in result["message"].lower()


@pytest.mark.asyncio
async def test_endpoint_returns_soft_error_on_atlas_exception(monkeypatch):
    from backend.routes import parabolic_phase_admin

    monkeypatch.setattr(
        parabolic_phase_admin, "db",
        _FakeDB("raise"),
    )

    result = await parabolic_phase_admin.get_phase_counts(_user={})
    assert result["error"] == "mongo_error"
    assert result["total_classified"] == 0
    # Exception type surfaced so operator can tell what went wrong.
    assert "RuntimeError" in result["message"]


@pytest.mark.asyncio
async def test_endpoint_returns_counts_on_happy_path(monkeypatch):
    from backend.routes import parabolic_phase_admin

    rows = [
        {"_id": "AMD", "snapshot": {"parabolic_phase": "parabolic"}},
        {"_id": "TSLA", "snapshot": {"parabolic_phase": "topping"}},
        {"_id": "NVDA", "snapshot": {"parabolic_phase": "accumulation"}},
        {"_id": "SPY",  "snapshot": {"parabolic_phase": "fade"}},
        # A row with an unknown phase must land in `unknown`, not crash.
        {"_id": "XYZ",  "snapshot": {"parabolic_phase": "gibberish"}},
        # A row with no snapshot at all also lands in `unknown`.
        {"_id": "ABC",  "snapshot": None},
    ]
    monkeypatch.setattr(
        parabolic_phase_admin, "db",
        _FakeDB("ok", rows),
    )

    result = await parabolic_phase_admin.get_phase_counts(_user={})
    assert "error" not in result
    assert result["counts"]["parabolic"] == 1
    assert result["counts"]["topping"] == 1
    assert result["counts"]["accumulation"] == 1
    assert result["counts"]["fade"] == 1
    assert result["counts"]["unknown"] == 2
    assert result["total_classified"] == 6


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
