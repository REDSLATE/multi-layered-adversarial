"""Tests for the Alpha Vantage cached feeder.

Doctrine pin: AV free tier = 25 calls / UTC day. This feeder is the
SOLE egress to alphavantage.co. Every consumer must route through
`get_payload()` so the cache + quota protections apply uniformly.

These tests exercise the cache-hit, cache-miss-fetch, quota-exhausted,
AV rate-limit body, error-message, and force-refresh paths against
a real Mongo instance with monkeypatched httpx so no network calls
are made.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

from db import db
from namespaces import ALPHA_VANTAGE_CACHE, ALPHA_VANTAGE_QUOTA
from shared.feeders import alpha_vantage as av


@pytest.fixture(autouse=True)
async def _clear_av_state():
    """Each test starts with empty cache + quota."""
    await db[ALPHA_VANTAGE_CACHE].delete_many({})
    await db[ALPHA_VANTAGE_QUOTA].delete_many({})
    yield
    await db[ALPHA_VANTAGE_CACHE].delete_many({})
    await db[ALPHA_VANTAGE_QUOTA].delete_many({})


@pytest.fixture
def _av_key(monkeypatch):
    monkeypatch.setenv("ALPHA_VANTAGE_API_KEY", "test-key-xyz")
    monkeypatch.delenv("ALPHA_VANTAGE_DAILY_CAP", raising=False)
    return "test-key-xyz"


class _FakeResp:
    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code
        self.text = "fake-body"

    def json(self):
        return self._payload


def _patch_httpx(monkeypatch, response: _FakeResp, *, raises=None):
    """Replace httpx.AsyncClient with one that returns `response`."""
    calls = []

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            self.kwargs = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, params=None):
            calls.append({"url": url, "params": dict(params or {})})
            if raises is not None:
                raise raises
            return response

    monkeypatch.setattr(av.httpx, "AsyncClient", _FakeClient)
    return calls


async def test_cache_hit_returns_payload_without_quota_use(_av_key, monkeypatch):
    """A pre-existing cache row for today is served WITHOUT any AV
    network call AND without bumping the quota counter."""
    today = av._today_utc()
    await db[ALPHA_VANTAGE_CACHE].update_one(
        {"symbol": "AAPL", "function": "OVERVIEW", "date": today},
        {"$set": {
            "symbol": "AAPL", "function": "OVERVIEW", "date": today,
            "payload": {"Symbol": "AAPL", "Sector": "TECHNOLOGY"},
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }},
        upsert=True,
    )
    calls = _patch_httpx(monkeypatch, _FakeResp({"unused": True}))

    res = await av.get_payload("AAPL", "OVERVIEW")
    assert res["ok"] is True
    assert res["from_cache"] is True
    assert res["payload"] == {"Symbol": "AAPL", "Sector": "TECHNOLOGY"}
    assert res["quota_used_today"] == 0
    assert calls == []  # zero network round-trips
    # And the quota counter was never created.
    q = await db[ALPHA_VANTAGE_QUOTA].find_one({"_id": today})
    assert q is None


async def test_cache_miss_fetches_and_increments_quota(_av_key, monkeypatch):
    """A miss triggers exactly one AV call, caches the payload, and
    bumps the quota counter by 1."""
    today = av._today_utc()
    calls = _patch_httpx(monkeypatch, _FakeResp({"Symbol": "MSFT"}))

    res = await av.get_payload("MSFT", "OVERVIEW")
    assert res["ok"] is True
    assert res["from_cache"] is False
    assert res["payload"] == {"Symbol": "MSFT"}
    assert res["quota_used_today"] == 1
    assert len(calls) == 1
    assert calls[0]["params"]["function"] == "OVERVIEW"
    assert calls[0]["params"]["symbol"] == "MSFT"
    assert calls[0]["params"]["apikey"] == "test-key-xyz"

    # Cache row persisted.
    row = await db[ALPHA_VANTAGE_CACHE].find_one(
        {"symbol": "MSFT", "function": "OVERVIEW", "date": today},
    )
    assert row is not None
    assert row["payload"] == {"Symbol": "MSFT"}

    # Quota counter at 1.
    q = await db[ALPHA_VANTAGE_QUOTA].find_one({"_id": today})
    assert q is not None
    assert q["count"] == 1

    # A second call hits the cache, quota stays at 1.
    res2 = await av.get_payload("MSFT", "OVERVIEW")
    assert res2["from_cache"] is True
    assert res2["quota_used_today"] == 1


async def test_quota_exhausted_blocks_network_call(_av_key, monkeypatch):
    """Once the counter is at the cap, the feeder refuses to call AV
    and returns `quota_exhausted` WITHOUT a network round-trip."""
    today = av._today_utc()
    cap = av._daily_cap()
    await db[ALPHA_VANTAGE_QUOTA].update_one(
        {"_id": today},
        {"$set": {"count": cap, "first_call_at": "x", "last_call_at": "y"}},
        upsert=True,
    )
    calls = _patch_httpx(monkeypatch, _FakeResp({"Symbol": "AAPL"}))

    res = await av.get_payload("AAPL", "OVERVIEW")
    assert res["ok"] is False
    assert res["error"] == "quota_exhausted"
    assert res["quota_used_today"] == cap
    assert calls == []


async def test_av_rate_limit_body_marks_quota_full(_av_key, monkeypatch):
    """When AV returns 200 with a `Note` body indicating rate-limit,
    we MUST treat the daily cap as exhausted so we don't burn more
    quota in a hot loop."""
    today = av._today_utc()
    rate_limit_body = {
        "Note": (
            "Thank you for using Alpha Vantage! Our standard API rate "
            "limit is 25 requests per day. Please subscribe to a "
            "premium plan to instantly remove all daily rate limits."
        )
    }
    _patch_httpx(monkeypatch, _FakeResp(rate_limit_body))

    res = await av.get_payload("TSLA", "OVERVIEW")
    assert res["ok"] is False
    assert res["error"] == "quota_exhausted"
    # Local counter pinned to cap so subsequent callers fail-fast.
    q = await db[ALPHA_VANTAGE_QUOTA].find_one({"_id": today})
    assert q is not None
    assert q["count"] == av._daily_cap()


async def test_av_error_message_surfaces_upstream_error(_av_key, monkeypatch):
    """AV's `Error Message` body is treated as `upstream_error` —
    NOT a quota event; the consumer chooses how to degrade."""
    _patch_httpx(monkeypatch, _FakeResp({"Error Message": "Invalid API call"}))

    res = await av.get_payload("BADSYM", "OVERVIEW")
    assert res["ok"] is False
    assert res["error"] == "upstream_error"
    # Quota counter NOT bumped — we only pay for successful payloads.
    q = await db[ALPHA_VANTAGE_QUOTA].find_one({"_id": av._today_utc()})
    assert q is None or q.get("count", 0) == 0


async def test_no_api_key_returns_soft_error(monkeypatch):
    monkeypatch.delenv("ALPHA_VANTAGE_API_KEY", raising=False)
    res = await av.get_payload("AAPL", "OVERVIEW")
    assert res["ok"] is False
    assert res["error"] == "no_api_key"


async def test_force_refresh_bypasses_cache(_av_key, monkeypatch):
    """`force_refresh=True` re-fetches even when a cache row exists,
    overwrites the cache, and bumps the quota."""
    today = av._today_utc()
    await db[ALPHA_VANTAGE_CACHE].update_one(
        {"symbol": "NVDA", "function": "OVERVIEW", "date": today},
        {"$set": {
            "symbol": "NVDA", "function": "OVERVIEW", "date": today,
            "payload": {"old": True},
            "fetched_at": "2026-01-01T00:00:00+00:00",
        }},
        upsert=True,
    )
    calls = _patch_httpx(monkeypatch, _FakeResp({"new": True}))

    res = await av.get_payload("NVDA", "OVERVIEW", force_refresh=True)
    assert res["ok"] is True
    assert res["from_cache"] is False
    assert res["payload"] == {"new": True}
    assert len(calls) == 1
    # Cache overwritten with new payload.
    row = await db[ALPHA_VANTAGE_CACHE].find_one(
        {"symbol": "NVDA", "function": "OVERVIEW", "date": today},
    )
    assert row["payload"] == {"new": True}
    # Quota bumped (force_refresh still costs).
    assert res["quota_used_today"] == 1


async def test_cap_can_be_raised_via_env(_av_key, monkeypatch):
    """Operator can lift the cap by setting `ALPHA_VANTAGE_DAILY_CAP`
    once they upgrade tiers — no code change required."""
    monkeypatch.setenv("ALPHA_VANTAGE_DAILY_CAP", "500")
    assert av._daily_cap() == 500


async def test_quota_state_endpoint(_av_key):
    """`quota_state()` reports remaining quota correctly for the day."""
    today = av._today_utc()
    await db[ALPHA_VANTAGE_QUOTA].update_one(
        {"_id": today},
        {"$set": {"count": 7, "first_call_at": "a", "last_call_at": "b"}},
        upsert=True,
    )
    state = await av.quota_state()
    assert state["date"] == today
    assert state["used"] == 7
    assert state["cap"] == 25
    assert state["remaining"] == 18


async def test_cache_prune_drops_old_rows(_av_key, monkeypatch):
    """Rows older than retention window are dropped on the next miss
    fetch — keeps the collection bounded."""
    today = av._today_utc()
    old_date = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")
    await db[ALPHA_VANTAGE_CACHE].update_one(
        {"symbol": "OLD", "function": "OVERVIEW", "date": old_date},
        {"$set": {
            "symbol": "OLD", "function": "OVERVIEW", "date": old_date,
            "payload": {"stale": True},
            "fetched_at": "2025-01-01T00:00:00+00:00",
        }},
        upsert=True,
    )
    _patch_httpx(monkeypatch, _FakeResp({"fresh": True}))

    await av.get_payload("AAPL", "OVERVIEW")
    # Background prune task is fire-and-forget; give it a tick.
    import asyncio
    await asyncio.sleep(0.2)

    old_row = await db[ALPHA_VANTAGE_CACHE].find_one(
        {"symbol": "OLD", "function": "OVERVIEW", "date": old_date},
    )
    assert old_row is None, "stale cache row should be pruned"
    fresh_row = await db[ALPHA_VANTAGE_CACHE].find_one(
        {"symbol": "AAPL", "function": "OVERVIEW", "date": today},
    )
    assert fresh_row is not None
