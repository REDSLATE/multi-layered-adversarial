"""Live learning loop — Stage 1 tests.

Covers:
    * `should_learn_from` predicate (all rules).
    * `capture_experience` write shape (fills, rejects, upsert).
    * `resolve_pending_outcomes` — 5m/15m/1h horizon walk, bps math,
      win flag, missing-mark skip, missing-entry skip.
    * `/api/admin/learning/*` endpoints roundtrip.

The shared prod-test Mongo is used, so every synthetic intent/
experience uses a `learn-test-` prefix and gets purged on teardown.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient, ASGITransport

sys.path.insert(0, "/app/backend")

from db import db  # noqa: E402
from server import app  # noqa: E402
from shared.learning.live_loop import (  # noqa: E402
    LEARNING_EXPERIENCES,
    capture_experience,
    should_learn_from,
)
from shared.learning.outcome_resolver import (  # noqa: E402
    _bps,
    resolve_pending_outcomes,
)


_TEST_PREFIX = "learn-test-"


@pytest.fixture(autouse=True)
async def _purge():
    """Purge synthetic learning rows before + after each test."""
    await db[LEARNING_EXPERIENCES].delete_many(
        {"intent_id": {"$regex": f"^{_TEST_PREFIX}"}},
    )
    yield
    await db[LEARNING_EXPERIENCES].delete_many(
        {"intent_id": {"$regex": f"^{_TEST_PREFIX}"}},
    )


# ─── should_learn_from ─────────────────────────────────────────────

def test_should_learn_from_direct_buy_with_notional():
    """Directional BUY with $5 micro-notional on equity lane → learn."""
    assert should_learn_from({
        "execution": {"action": "BUY", "notional_usd": 5.0},
        "lane": "equity",
    }) is True


def test_should_learn_from_direct_sell_crypto():
    assert should_learn_from({
        "execution": {"action": "SELL", "notional_usd": 1.0},
        "lane": "crypto",
    }) is True


def test_should_learn_from_rejects_hold_intent():
    """HOLD intents don't touch the market — no learning value."""
    assert should_learn_from({
        "execution": {"action": "HOLD", "notional_usd": 5.0},
        "lane": "equity",
    }) is False


def test_should_learn_from_rejects_zero_notional():
    """Zero-notional means no market exposure — nothing to learn."""
    assert should_learn_from({
        "execution": {"action": "BUY", "notional_usd": 0.0},
        "lane": "equity",
    }) is False


def test_should_learn_from_rejects_unknown_lane():
    """Only equity + crypto are recognised lanes; dev synthetic
    lanes (`test`, `paper`, `forex`) are not real exposure."""
    assert should_learn_from({
        "execution": {"action": "BUY", "notional_usd": 5.0},
        "lane": "forex",
    }) is False


def test_should_learn_from_falls_back_to_top_level_action():
    """When `execution.action` is missing, top-level `action` counts.
    Covers legacy intents that never went through V3 sizing."""
    assert should_learn_from({
        "action": "BUY",
        "execution": {"notional_usd": 5.0},
        "lane": "equity",
    }) is True


# ─── capture_experience ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_capture_writes_row_and_returns_intent_id():
    """Successful capture writes a single upserted row and returns
    the intent_id."""
    intent_id = f"{_TEST_PREFIX}cap-1"
    ret = await capture_experience(
        db,
        intent={
            "intent_id": intent_id,
            "symbol": "NVDA",
            "lane": "equity",
            "action": "BUY",
            "stack": "camino",
            "execution": {"action": "BUY", "notional_usd": 5.0},
            "notional_source": "micro_default",
            "snapshot": {"last_price": 195.0, "vwap": 194.5},
            "doctrine_packet": {"seats": {"execution_judge": {}}},
        },
        broker_receipt={
            "status": "submitted", "broker": "webull",
            "id": "WB-42", "filled_avg_price": None,
        },
        terminal_state="submitted",
    )
    assert ret == intent_id
    doc = await db[LEARNING_EXPERIENCES].find_one({"intent_id": intent_id})
    assert doc is not None
    assert doc["symbol"] == "NVDA"
    assert doc["lane"] == "equity"
    assert doc["action"] == "BUY"
    assert doc["notional_usd"] == 5.0
    assert doc["notional_source"] == "micro_default"
    assert doc["terminal_state"] == "submitted"
    assert doc["broker"] == "webull"
    assert doc["broker_order_id"] == "WB-42"
    assert doc["entry_price"] == 195.0
    # Outcome fields start null — the resolver fills them later.
    assert doc["outcome_5m_bps"] is None
    assert doc["outcome_15m_bps"] is None
    assert doc["outcome_1h_bps"] is None
    assert doc["win"] is None
    # Features + doctrine copied verbatim.
    assert doc["features"]["last_price"] == 195.0
    assert doc["doctrine"]["seats"] == {"execution_judge": {}}


@pytest.mark.asyncio
async def test_capture_reject_stamps_reject_reason():
    """Broker-reject captures include `reject_reason` + entry price
    from the snapshot (so 'what if it had filled' analysis works)."""
    intent_id = f"{_TEST_PREFIX}reject-1"
    await capture_experience(
        db,
        intent={
            "intent_id": intent_id, "symbol": "ADA/USD", "lane": "crypto",
            "action": "SELL",
            "execution": {"action": "SELL", "notional_usd": 5.0},
            "snapshot": {"last_price": 0.4321},
            "doctrine_packet": {},
        },
        broker_receipt={
            "status": "rejected", "broker": "kraken",
            "error_bucket": "min_order_notional",
            "error_detail": {"floor": 5, "requested": 1},
        },
        terminal_state="broker_rejected",
        reject_reason="notional_below_pair_floor",
    )
    doc = await db[LEARNING_EXPERIENCES].find_one({"intent_id": intent_id})
    assert doc["terminal_state"] == "broker_rejected"
    assert doc["reject_reason"] == "notional_below_pair_floor"
    # Entry price still stamped from snapshot even though we didn't fill.
    assert doc["entry_price"] == 0.4321


@pytest.mark.asyncio
async def test_capture_skips_when_should_learn_false():
    """`should_learn_from` returning False → no row written."""
    intent_id = f"{_TEST_PREFIX}skip-1"
    ret = await capture_experience(
        db,
        intent={
            "intent_id": intent_id, "symbol": "NVDA", "lane": "equity",
            "action": "HOLD",
            "execution": {"action": "HOLD", "notional_usd": 5.0},
        },
        terminal_state="submitted",
    )
    assert ret is None
    doc = await db[LEARNING_EXPERIENCES].find_one({"intent_id": intent_id})
    assert doc is None


@pytest.mark.asyncio
async def test_capture_upserts_on_repeat():
    """Same intent_id captured twice → single row, later state wins.
    (Handles the retry-then-fill flow where a reject was captured
    first and later replaced by the eventual fill.)"""
    intent_id = f"{_TEST_PREFIX}upsert-1"
    base = {
        "intent_id": intent_id, "symbol": "NVDA", "lane": "equity",
        "action": "BUY",
        "execution": {"action": "BUY", "notional_usd": 5.0},
        "snapshot": {"last_price": 200.0},
    }
    # First: reject
    await capture_experience(
        db, intent=base,
        broker_receipt={"status": "rejected"},
        terminal_state="broker_rejected",
        reject_reason="broker_transient",
    )
    # Then: fill
    await capture_experience(
        db, intent=base,
        broker_receipt={
            "status": "submitted", "broker": "webull", "id": "WB-99",
            "filled_avg_price": 201.5,
        },
        terminal_state="submitted",
    )
    n = await db[LEARNING_EXPERIENCES].count_documents(
        {"intent_id": intent_id},
    )
    assert n == 1
    doc = await db[LEARNING_EXPERIENCES].find_one({"intent_id": intent_id})
    assert doc["terminal_state"] == "submitted"
    assert doc["fill_price"] == 201.5


# ─── outcome_resolver ──────────────────────────────────────────────

def test_bps_arithmetic_buy_up_positive():
    """BUY entry $100, mark $105 → +500 bps (positive for us)."""
    assert _bps(100.0, 105.0, "BUY") == pytest.approx(500.0)


def test_bps_arithmetic_sell_down_positive():
    """SELL entry $100, mark $95 → +500 bps (we shorted the drop)."""
    assert _bps(100.0, 95.0, "SELL") == pytest.approx(500.0)


def test_bps_arithmetic_buy_down_negative():
    """BUY entry $100, mark $98 → -200 bps."""
    assert _bps(100.0, 98.0, "BUY") == pytest.approx(-200.0)


def test_bps_arithmetic_handles_zero_entry():
    """Divide-by-zero guard — a bad entry price must return 0, not crash."""
    assert _bps(0.0, 105.0, "BUY") == 0.0


@pytest.mark.asyncio
async def test_resolver_walks_all_three_horizons(monkeypatch):
    """A 65-minute-old experience with a valid entry price + a
    fetchable mark price gets ALL THREE horizon fields populated
    and `win` set in one sweep."""
    intent_id = f"{_TEST_PREFIX}resolve-1"
    # Insert an experience that's already older than 1h.
    old_ts = (datetime.now(timezone.utc) - timedelta(minutes=65)).isoformat()
    await db[LEARNING_EXPERIENCES].insert_one({
        "intent_id": intent_id, "symbol": "BTC/USD", "lane": "crypto",
        "action": "BUY", "notional_usd": 5.0,
        "entry_price": 60000.0, "fill_price": 60000.0,
        "terminal_state": "submitted",
        "created_at": old_ts,
        "outcome_5m_bps": None, "outcome_15m_bps": None,
        "outcome_1h_bps": None,
        "outcome_resolved_at_5m": None, "outcome_resolved_at_15m": None,
        "outcome_resolved_at_1h": None,
        "win": None,
        "features": {}, "doctrine": {},
    })

    # Force the mark fetcher to return 61200 → +200 bps for a BUY.
    async def _fake_mark(lane, symbol):
        return 61200.0

    monkeypatch.setattr(
        "shared.learning.outcome_resolver._fetch_mark_price",
        _fake_mark,
    )
    counts = await resolve_pending_outcomes(db)
    assert counts["resolved_5m"] >= 1
    assert counts["resolved_15m"] >= 1
    assert counts["resolved_1h"] >= 1

    doc = await db[LEARNING_EXPERIENCES].find_one({"intent_id": intent_id})
    assert doc["outcome_5m_bps"] == pytest.approx(200.0)
    assert doc["outcome_15m_bps"] == pytest.approx(200.0)
    assert doc["outcome_1h_bps"] == pytest.approx(200.0)
    assert doc["win"] is True


@pytest.mark.asyncio
async def test_resolver_skips_when_mark_price_missing(monkeypatch):
    """Equity rows currently return `None` from `_fetch_mark_price`
    (Stage 2 will wire equity feeds). Resolver must NOT crash — it
    counts `skipped_missing_mark` and leaves the row unchanged."""
    intent_id = f"{_TEST_PREFIX}resolve-nomark"
    old_ts = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    await db[LEARNING_EXPERIENCES].insert_one({
        "intent_id": intent_id, "symbol": "NVDA", "lane": "equity",
        "action": "BUY", "notional_usd": 5.0, "entry_price": 200.0,
        "created_at": old_ts,
        "outcome_5m_bps": None, "outcome_15m_bps": None,
        "outcome_1h_bps": None, "win": None,
        "terminal_state": "submitted",
        "features": {}, "doctrine": {},
    })

    async def _fake_mark(lane, symbol):
        return None  # simulate missing feed

    monkeypatch.setattr(
        "shared.learning.outcome_resolver._fetch_mark_price",
        _fake_mark,
    )
    counts = await resolve_pending_outcomes(db)
    assert counts["skipped_missing_mark"] >= 1

    doc = await db[LEARNING_EXPERIENCES].find_one({"intent_id": intent_id})
    assert doc["outcome_5m_bps"] is None
    assert doc["win"] is None


# ─── admin routes ─────────────────────────────────────────────────

async def _login(client) -> str:
    r = await client.post(
        "/api/auth/login",
        json={"email": "admin@risedual.io",
              "password": "risedual-admin-2026"},
    )
    return r.json().get("access_token") or r.json().get("token")


@pytest.mark.asyncio
async def test_stats_endpoint_returns_counts():
    """`GET /admin/learning/stats` returns the multi-horizon backlog
    + hit-rate summary. Structure-only assertion — the underlying
    counts include prod rows."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        tok = await _login(client)
        r = await client.get(
            "/api/admin/learning/stats",
            headers={"Authorization": f"Bearer {tok}"},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert "total_experiences" in body
    assert set(body["pending_outcomes"].keys()) == {"5m", "15m", "1h"}
    assert "by_lane" in body


@pytest.mark.asyncio
async def test_experiences_endpoint_returns_test_row():
    """`GET /admin/learning/experiences` returns our synthetic row
    when filtered to it. Confirms end-to-end read path."""
    intent_id = f"{_TEST_PREFIX}route-1"
    await capture_experience(
        db,
        intent={
            "intent_id": intent_id, "symbol": "SPY", "lane": "equity",
            "action": "BUY",
            "execution": {"action": "BUY", "notional_usd": 5.0},
            "snapshot": {"last_price": 745.0},
        },
        terminal_state="submitted",
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        tok = await _login(client)
        r = await client.get(
            "/api/admin/learning/experiences?lane=equity&limit=100",
            headers={"Authorization": f"Bearer {tok}"},
        )
    assert r.status_code == 200
    body = r.json()
    ours = [x for x in body["items"] if x["intent_id"] == intent_id]
    assert len(ours) == 1
    assert ours[0]["symbol"] == "SPY"
    # `features` + `doctrine` are excluded from the list endpoint to
    # keep the payload small.
    assert "features" not in ours[0]
