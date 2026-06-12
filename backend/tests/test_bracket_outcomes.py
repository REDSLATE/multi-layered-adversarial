"""Tests for the bracket outcome resolver + recorder (P1, 2026-02-19).

The training-signal upgrade: every brain intent that carries
target_price+stop_price gets a categorical outcome label
(`tp_hit`/`sl_hit`/`timeout`) instead of the old PnL-thresholded
win/loss/scratch. These tests pin:

  * Resolver math — directional cases for BUY and SELL.
  * Optimistic resolution on simultaneous breach (TP wins ties —
    doctrine: honor the brain's published conviction on ambiguity).
  * Timeout fires only when neither threshold breached AND now > expires.
  * Outcome-join mirror writes the correct label + pnl.
  * Recorder skips intents without target/stop (legacy path preserved).
  * Recorder REJECTS malformed brackets (BUY where stop > entry, etc.)
    — wrong direction = no write, log warning.
  * Recorder is a no-op when the master kill-switch is off.
  * The endpoint binning math (`tp_rate per confidence band`) is right.
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from dotenv import load_dotenv  # noqa: E402

load_dotenv("/app/backend/.env")

from db import db  # noqa: E402
from namespaces import WEBULL_BRACKET_INTENTS, DOCTRINE_SIDECARS  # noqa: E402
from shared.broker.webull_brackets import (  # noqa: E402
    record_bracket_intent, bracket_outcomes_enabled,
)
from shared.runtime.bracket_outcome_resolver import (  # noqa: E402
    _resolve_outcome, _pnl_for_resolution,
    resolve_open_brackets_once,
)


# ── pure logic: outcome resolution math ───────────────────────────


class TestResolveOutcome:
    def test_buy_tp_hit(self):
        assert _resolve_outcome("BUY", 105.0, target=104.0, stop=98.0, expired=False) == "tp_hit"

    def test_buy_sl_hit(self):
        assert _resolve_outcome("BUY", 97.0, target=104.0, stop=98.0, expired=False) == "sl_hit"

    def test_buy_between_stays_open(self):
        assert _resolve_outcome("BUY", 100.0, target=104.0, stop=98.0, expired=False) is None

    def test_buy_between_but_expired_is_timeout(self):
        assert _resolve_outcome("BUY", 100.0, target=104.0, stop=98.0, expired=True) == "timeout"

    def test_buy_simultaneous_breach_favors_tp(self):
        """Doctrine: on a price spike that touches both legs, honor the
        brain's published conviction (TP wins ties)."""
        # Price hit TP exactly; assume SL was also crossed earlier
        # in the same tick window — TP wins.
        assert _resolve_outcome("BUY", 104.0, target=104.0, stop=104.0, expired=True) == "tp_hit"

    def test_sell_tp_hit(self):
        """Short bracket: favorable = price DOWN."""
        assert _resolve_outcome("SELL", 95.0, target=96.0, stop=102.0, expired=False) == "tp_hit"

    def test_sell_sl_hit(self):
        assert _resolve_outcome("SELL", 103.0, target=96.0, stop=102.0, expired=False) == "sl_hit"

    def test_sell_between_stays_open(self):
        assert _resolve_outcome("SELL", 100.0, target=96.0, stop=102.0, expired=False) is None

    def test_unknown_side_never_resolves(self):
        assert _resolve_outcome("FLIP", 99.0, target=104.0, stop=98.0, expired=True) is None


class TestPnlForResolution:
    def test_buy_tp_pnl_positive(self):
        # Long: bought at 100, resolved at 104, qty=2 → +8.
        assert _pnl_for_resolution("BUY", qty=2, entry=100.0, resolved_at_price=104.0) == pytest.approx(8.0)

    def test_buy_sl_pnl_negative(self):
        assert _pnl_for_resolution("BUY", qty=2, entry=100.0, resolved_at_price=97.0) == pytest.approx(-6.0)

    def test_sell_tp_pnl_positive(self):
        # Short: sold at 100, covered at 95, qty=2 → +10.
        assert _pnl_for_resolution("SELL", qty=2, entry=100.0, resolved_at_price=95.0) == pytest.approx(10.0)

    def test_sell_sl_pnl_negative(self):
        assert _pnl_for_resolution("SELL", qty=2, entry=100.0, resolved_at_price=103.0) == pytest.approx(-6.0)


# ── recorder: write side ──────────────────────────────────────────


@pytest.fixture
def isolated_intent_id():
    iid = f"bracket-test-{uuid.uuid4().hex[:10]}"
    yield iid
    async def _cleanup():
        await db[WEBULL_BRACKET_INTENTS].delete_many({"intent_id": iid})
        await db[DOCTRINE_SIDECARS].delete_many({"intent_id": iid})
    asyncio.get_event_loop().run_until_complete(_cleanup())


@pytest.mark.asyncio
async def test_recorder_skips_when_kill_switch_off(
    monkeypatch, isolated_intent_id,
):
    """Master kill-switch off → recorder is a no-op. Critical: the
    default config (no env var set) must NOT silently start writing
    brackets."""
    monkeypatch.delenv("RISEDUAL_BRACKET_OUTCOMES_ENABLED", raising=False)
    assert bracket_outcomes_enabled() is False
    intent = {
        "intent_id": isolated_intent_id, "stack": "alpha", "symbol": "NVDA",
        "lane": "equity", "action": "BUY", "confidence": 0.7,
        "target_price": 140.0, "stop_price": 135.0,
    }
    order = {"order_id": "X", "client_order_id": "X", "qty": 0.07, "notional": 10.0, "side": "BUY"}
    result = await record_bracket_intent(intent, order, entry_price=138.0)
    assert result is None
    assert await db[WEBULL_BRACKET_INTENTS].count_documents(
        {"intent_id": isolated_intent_id},
    ) == 0


@pytest.mark.asyncio
async def test_recorder_skips_when_no_target_stop(monkeypatch, isolated_intent_id):
    """Intent without bracket fields → legacy outcome path; no row written."""
    monkeypatch.setenv("RISEDUAL_BRACKET_OUTCOMES_ENABLED", "true")
    intent = {
        "intent_id": isolated_intent_id, "stack": "alpha", "symbol": "AAPL",
        "lane": "equity", "action": "BUY", "confidence": 0.7,
    }
    result = await record_bracket_intent(intent, {"qty": 0.04, "notional": 10.0}, entry_price=225.0)
    assert result is None


@pytest.mark.asyncio
async def test_recorder_rejects_malformed_buy_bracket(monkeypatch, isolated_intent_id):
    """For a BUY: target MUST be above entry, stop MUST be below.
    Reject inverted brackets — they'd cause the resolver to fire the
    wrong leg."""
    monkeypatch.setenv("RISEDUAL_BRACKET_OUTCOMES_ENABLED", "true")
    intent = {
        "intent_id": isolated_intent_id, "stack": "alpha", "symbol": "NVDA",
        "lane": "equity", "action": "BUY", "confidence": 0.7,
        "target_price": 130.0,  # below entry
        "stop_price": 145.0,    # above entry — backwards
    }
    result = await record_bracket_intent(intent, {"qty": 0.07, "notional": 10.0}, entry_price=138.0)
    assert result is None


@pytest.mark.asyncio
async def test_recorder_writes_well_formed_buy_bracket(monkeypatch, isolated_intent_id):
    """Happy path: BUY with valid target>entry>stop persists, returns
    a bracket_id, captures everything the resolver needs."""
    monkeypatch.setenv("RISEDUAL_BRACKET_OUTCOMES_ENABLED", "true")
    intent = {
        "intent_id": isolated_intent_id, "stack": "alpha", "symbol": "NVDA",
        "lane": "equity", "action": "BUY", "confidence": 0.78,
        "target_price": 142.0, "stop_price": 135.0,
    }
    order = {
        "order_id": "WB-1", "client_order_id": "CID-1", "qty": 0.07,
        "notional": 10.0, "side": "BUY", "broker": "webull",
    }
    bracket_id = await record_bracket_intent(intent, order, entry_price=138.5)
    assert bracket_id is not None
    row = await db[WEBULL_BRACKET_INTENTS].find_one(
        {"bracket_id": bracket_id}, {"_id": 0},
    )
    assert row["status"] == "open"
    assert row["entry_price"] == pytest.approx(138.5)
    assert row["target_price"] == pytest.approx(142.0)
    assert row["stop_price"] == pytest.approx(135.0)
    assert row["confidence"] == pytest.approx(0.78)
    assert row["broker_order_id"] == "WB-1"
    assert row["opened_at"] < row["expires_at"]


# ── resolver: end-to-end ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_resolver_marks_tp_hit_and_mirrors_to_outcome_join(
    monkeypatch, isolated_intent_id,
):
    """Seed a doctrine sidecar + an open bracket, point the price
    fetcher above target → resolver writes tp_hit AND mirrors the
    label into outcome_join."""
    monkeypatch.setenv("RISEDUAL_BRACKET_OUTCOMES_ENABLED", "true")
    # Pre-seed the doctrine sidecar so the mirror has a row to upsert.
    await db[DOCTRINE_SIDECARS].insert_one({
        "intent_id": isolated_intent_id,
        "lane": "equity", "doctrine_version": "test_v1",
        "stack": "alpha", "symbol": "NVDA",
    })
    # Now seed the bracket.
    intent = {
        "intent_id": isolated_intent_id, "stack": "alpha", "symbol": "NVDA",
        "lane": "equity", "action": "BUY", "confidence": 0.7,
        "target_price": 142.0, "stop_price": 135.0,
    }
    await record_bracket_intent(intent, {"qty": 0.1, "notional": 14.0}, entry_price=140.0)

    async def _stub_price(symbol, lane):
        return 143.0  # above target → tp_hit

    counts = await resolve_open_brackets_once(price_fetcher=_stub_price)
    assert counts["tp_hit"] == 1

    # Bracket marked resolved.
    row = await db[WEBULL_BRACKET_INTENTS].find_one(
        {"intent_id": isolated_intent_id},
    )
    assert row["status"] == "resolved"
    assert row["outcome_label"] == "tp_hit"
    assert row["resolved_price"] == 143.0
    # pnl = (143 - 140) * 0.1 = 0.30
    assert row["pnl_usd"] == pytest.approx(0.30)

    # outcome_join mirrored into doctrine_sidecars.
    sidecar = await db[DOCTRINE_SIDECARS].find_one(
        {"intent_id": isolated_intent_id},
    )
    oj = sidecar.get("outcome_join")
    assert oj is not None
    assert oj["outcome_label"] == "tp_hit"
    assert oj["resolved_via"] == "bracket_outcome_resolver"
    assert oj["pnl_usd"] == pytest.approx(0.30)


@pytest.mark.asyncio
async def test_resolver_marks_sl_hit(monkeypatch, isolated_intent_id):
    monkeypatch.setenv("RISEDUAL_BRACKET_OUTCOMES_ENABLED", "true")
    intent = {
        "intent_id": isolated_intent_id, "stack": "alpha", "symbol": "NVDA",
        "lane": "equity", "action": "BUY", "confidence": 0.5,
        "target_price": 142.0, "stop_price": 135.0,
    }
    await record_bracket_intent(intent, {"qty": 0.1, "notional": 14.0}, entry_price=140.0)

    async def _stub_price(symbol, lane):
        return 134.0  # below stop

    counts = await resolve_open_brackets_once(price_fetcher=_stub_price)
    assert counts["sl_hit"] == 1
    row = await db[WEBULL_BRACKET_INTENTS].find_one({"intent_id": isolated_intent_id})
    assert row["outcome_label"] == "sl_hit"
    assert row["pnl_usd"] == pytest.approx((134.0 - 140.0) * 0.1)


@pytest.mark.asyncio
async def test_resolver_marks_timeout_when_expired_and_between(
    monkeypatch, isolated_intent_id,
):
    monkeypatch.setenv("RISEDUAL_BRACKET_OUTCOMES_ENABLED", "true")
    intent = {
        "intent_id": isolated_intent_id, "stack": "alpha", "symbol": "NVDA",
        "lane": "equity", "action": "BUY", "confidence": 0.4,
        "target_price": 142.0, "stop_price": 135.0,
    }
    await record_bracket_intent(intent, {"qty": 0.1, "notional": 14.0}, entry_price=140.0)
    # Force expiry by rewriting the bracket's expires_at into the past.
    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    await db[WEBULL_BRACKET_INTENTS].update_one(
        {"intent_id": isolated_intent_id},
        {"$set": {"expires_at": past}},
    )

    async def _stub_price(symbol, lane):
        return 138.0  # between target and stop

    counts = await resolve_open_brackets_once(price_fetcher=_stub_price)
    assert counts["timeout"] == 1
    row = await db[WEBULL_BRACKET_INTENTS].find_one({"intent_id": isolated_intent_id})
    assert row["outcome_label"] == "timeout"


@pytest.mark.asyncio
async def test_resolver_skips_on_price_fetch_failure(
    monkeypatch, isolated_intent_id,
):
    """When the price fetcher returns None (transient quotes API
    error), the bracket must NOT be silently misresolved as anything —
    it stays open for the next tick."""
    monkeypatch.setenv("RISEDUAL_BRACKET_OUTCOMES_ENABLED", "true")
    intent = {
        "intent_id": isolated_intent_id, "stack": "alpha", "symbol": "NVDA",
        "lane": "equity", "action": "BUY", "confidence": 0.6,
        "target_price": 142.0, "stop_price": 135.0,
    }
    await record_bracket_intent(intent, {"qty": 0.1, "notional": 14.0}, entry_price=140.0)

    async def _stub_price(symbol, lane):
        return None

    counts = await resolve_open_brackets_once(price_fetcher=_stub_price)
    assert counts["skipped"] == 1
    row = await db[WEBULL_BRACKET_INTENTS].find_one({"intent_id": isolated_intent_id})
    assert row["status"] == "open"   # untouched
    assert row["outcome_label"] is None


# ── diagnostics endpoint binning math ─────────────────────────────


def test_diagnostics_endpoint_bin_label_function():
    """Pure helper — pin the confidence bin boundaries."""
    from routes.admin_brackets import _bin_label
    assert _bin_label(0.1) == "0.0-0.3"
    assert _bin_label(0.3) == "0.3-0.5"
    assert _bin_label(0.5) == "0.5-0.7"
    assert _bin_label(0.7) == "0.7-0.85"
    assert _bin_label(0.85) == "0.85-1.0"
    assert _bin_label(1.0) == "0.85-1.0"
