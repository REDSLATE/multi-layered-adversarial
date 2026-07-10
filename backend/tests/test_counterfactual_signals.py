"""Counterfactual signal tests — distillation + resolver + verdict math.

Locks in operator doctrine (2026-02-19):
    * should_create_counterfactual predicate — only real directional
      intents that never reached the broker
    * distill_intent_to_signal writes ONE compact row with the
      required fields + is idempotent
    * signed_return_bps: BUY up +, BUY down -, SELL down +, SHORT down +
    * verdicts: MISSED_WIN >= 20 bps, CORRECT_BLOCK <= -20 bps,
      UNDETERMINED in between
    * resolver stamps outcomes.{5m/15m/1h}.{mark_price, return_bps,
      verdict, mark_source, resolved_at}
    * status flips to "resolved" once 1h horizon lands

All tests scoped to a test-prefix to avoid touching production.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, "/app/backend")

from db import db  # noqa: E402
from namespaces import COUNTERFACTUAL_SIGNALS  # noqa: E402
from shared import counterfactuals as cf  # noqa: E402


_PFX = "cf-signal-test-"


def _iso(dt): return dt.isoformat()
def _now(): return datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
async def _purge():
    await db[COUNTERFACTUAL_SIGNALS].delete_many(
        {"signal_id": {"$regex": f"^{_PFX}"}},
    )
    yield
    await db[COUNTERFACTUAL_SIGNALS].delete_many(
        {"signal_id": {"$regex": f"^{_PFX}"}},
    )


# ─── predicate ────────────────────────────────────────────────────


def test_predicate_true_for_directional_blocked():
    assert cf.should_create_counterfactual({
        "action": "BUY", "gate_state": "blocked",
        "executed": False,
    })


def test_predicate_true_for_no_trade_directional():
    assert cf.should_create_counterfactual({
        "action": "SELL", "gate_state": "no_trade",
    })


def test_predicate_true_for_expired_unrouted_directional():
    assert cf.should_create_counterfactual({
        "action": "SHORT", "gate_state": "expired_unrouted",
    })


def test_predicate_false_for_hold():
    assert not cf.should_create_counterfactual({
        "action": "HOLD", "gate_state": "no_trade",
    })


def test_predicate_false_when_executed():
    assert not cf.should_create_counterfactual({
        "action": "BUY", "gate_state": "blocked", "executed": True,
    })


def test_predicate_false_when_broker_order_id_present():
    assert not cf.should_create_counterfactual({
        "action": "BUY", "gate_state": "blocked",
        "broker_order_id": "webull-xyz",
    })


def test_predicate_false_when_gate_state_submitted():
    assert not cf.should_create_counterfactual({
        "action": "BUY", "gate_state": "submitted",
    })


def test_predicate_reads_execution_action():
    assert cf.should_create_counterfactual({
        "execution": {"action": "COVER"}, "gate_state": "blocked",
    })


# ─── signed_return_bps ────────────────────────────────────────────


def test_signed_return_buy_up_positive():
    assert cf.signed_return_bps("BUY", 100.0, 101.0) == pytest.approx(100.0)


def test_signed_return_sell_down_positive():
    """SELL when price DROPS is a WIN → positive bps."""
    assert cf.signed_return_bps("SELL", 100.0, 99.0) == pytest.approx(100.0)


def test_signed_return_short_down_positive():
    assert cf.signed_return_bps("SHORT", 100.0, 99.0) == pytest.approx(100.0)


def test_signed_return_cover_up_positive():
    assert cf.signed_return_bps("COVER", 100.0, 101.0) == pytest.approx(100.0)


def test_signed_return_zero_entry_safe():
    assert cf.signed_return_bps("BUY", 0.0, 101.0) == 0.0


# ─── verdict thresholds ──────────────────────────────────────────


def test_verdict_missed_win_at_threshold():
    assert cf._verdict_for(20.0) == "MISSED_WIN"
    assert cf._verdict_for(500.0) == "MISSED_WIN"


def test_verdict_correct_block_at_threshold():
    assert cf._verdict_for(-20.0) == "CORRECT_BLOCK"
    assert cf._verdict_for(-500.0) == "CORRECT_BLOCK"


def test_verdict_undetermined_in_deadband():
    assert cf._verdict_for(0.0) == "UNDETERMINED"
    assert cf._verdict_for(19.99) == "UNDETERMINED"
    assert cf._verdict_for(-19.99) == "UNDETERMINED"


# ─── distill_intent_to_signal ─────────────────────────────────────


@pytest.mark.asyncio
async def test_distill_writes_full_signal_doc():
    intent = {
        "intent_id": f"{_PFX}intent-1",
        "action": "BUY",
        "gate_state": "blocked",
        "symbol": "NVDA",
        "lane": "equity",
        "stack_canonical": "gto",
        "ingest_ts": _iso(_now() - timedelta(hours=2)),
        "snapshot": {
            "price": 182.40,
            "relative_volume": 1.12,
            "rvol_acceleration": 0.38,
            "vwap_distance_pct": 0.44,
            "market_regime": "bull",
        },
        "broker_reason": "relative_volume_below_threshold",
    }
    ok = await cf.distill_intent_to_signal(intent, db)
    assert ok is True
    doc = await db[COUNTERFACTUAL_SIGNALS].find_one(
        {"signal_id": f"{_PFX}intent-1"},
    )
    assert doc is not None
    assert doc["direction"] == "BUY"
    assert doc["symbol"] == "NVDA"
    assert doc["lane"] == "equity"
    assert doc["brain"] == "gto"
    assert doc["entry_reference_price"] == pytest.approx(182.40)
    assert doc["blocked_reason"] == "relative_volume_below_threshold"
    assert doc["status"] == "tracking"
    assert doc["features"]["relative_volume"] == 1.12
    assert doc["features"]["market_regime"] == "bull"
    assert doc["outcomes"] == {}
    # 2026-02-19 upgrade — belt-and-suspenders execution firewall.
    assert doc["may_execute"] is False
    assert doc["broker_access"] is False
    # Bucket-analyzer dimension separating counterfactuals from
    # executed learning experiences.
    assert doc["experience_type"] == "counterfactual"


@pytest.mark.asyncio
async def test_distill_supports_short_and_cover():
    """SHORT / COVER intents must qualify + get signed correctly."""
    for action in ("SHORT", "COVER"):
        intent_id = f"{_PFX}dir-{action.lower()}"
        intent = {
            "intent_id": intent_id,
            "action": action,
            "gate_state": "no_trade",
            "symbol": "TSLA",
            "lane": "equity",
            "snapshot": {"price": 240.0},
            "ingest_ts": _iso(_now()),
        }
        assert cf.should_create_counterfactual(intent) is True
        ok = await cf.distill_intent_to_signal(intent, db)
        assert ok is True
        doc = await db[COUNTERFACTUAL_SIGNALS].find_one(
            {"signal_id": intent_id},
        )
        assert doc is not None
        assert doc["direction"] == action
        assert doc["may_execute"] is False
        assert doc["broker_access"] is False


@pytest.mark.asyncio
async def test_distill_is_idempotent():
    intent = {
        "intent_id": f"{_PFX}idem",
        "action": "BUY", "gate_state": "blocked",
        "symbol": "AAPL", "lane": "equity",
        "snapshot": {"price": 190.0},
        "ingest_ts": _iso(_now()),
    }
    a = await cf.distill_intent_to_signal(intent, db)
    b = await cf.distill_intent_to_signal(intent, db)
    assert a is True and b is True
    n = await db[COUNTERFACTUAL_SIGNALS].count_documents(
        {"signal_id": f"{_PFX}idem"},
    )
    assert n == 1


@pytest.mark.asyncio
async def test_distill_returns_false_when_no_reference_price():
    """Missing price → we refuse to create a signal we can't score."""
    intent = {
        "intent_id": f"{_PFX}no-price",
        "action": "BUY", "gate_state": "blocked",
        "symbol": "AAPL",
        # No snapshot.price, no target_price.
        "ingest_ts": _iso(_now()),
    }
    ok = await cf.distill_intent_to_signal(intent, db)
    assert ok is False
    assert await db[COUNTERFACTUAL_SIGNALS].find_one(
        {"signal_id": f"{_PFX}no-price"}
    ) is None


@pytest.mark.asyncio
async def test_distill_returns_false_when_predicate_fails():
    intent = {
        "intent_id": f"{_PFX}hold",
        "action": "HOLD", "gate_state": "no_trade",
        "snapshot": {"price": 100.0},
    }
    ok = await cf.distill_intent_to_signal(intent, db)
    assert ok is False


# ─── resolver: end-to-end ────────────────────────────────────────


@pytest.mark.asyncio
async def test_resolver_stamps_outcomes_and_verdict(monkeypatch):
    """Seed a signal that's older than 1h, mock the mark to +50 bps
    over entry, run resolver, verify 5m/15m/1h all stamped with
    MISSED_WIN, status flipped to resolved."""
    from shared.learning.outcome_resolver import MarkQuote

    async def _fresh_quote(lane, symbol):
        return MarkQuote(
            price=201.0, source="webull_last_trade",
            ts=_iso(_now()), is_stale=False,
        )

    monkeypatch.setattr(
        "shared.learning.outcome_resolver._fetch_mark_quote",
        _fresh_quote,
    )

    old_ts = _iso(_now() - timedelta(hours=2))
    await db[COUNTERFACTUAL_SIGNALS].insert_one({
        "signal_id": f"{_PFX}res-1",
        "source_intent_id": f"{_PFX}res-1",
        "symbol": "TSLA", "lane": "equity",
        "direction": "BUY",
        "entry_reference_price": 200.0,
        "status": "tracking",
        "outcomes": {},
        "created_at": old_ts,
    })

    counts = await cf.resolve_pending_signals(db)
    assert counts["resolved_5m"] >= 1
    assert counts["resolved_15m"] >= 1
    assert counts["resolved_1h"] >= 1

    doc = await db[COUNTERFACTUAL_SIGNALS].find_one(
        {"signal_id": f"{_PFX}res-1"},
    )
    # +50 bps → MISSED_WIN at all three horizons.
    for h in ("5m", "15m", "1h"):
        assert doc["outcomes"][h]["verdict"] == "MISSED_WIN"
        assert doc["outcomes"][h]["return_bps"] == pytest.approx(50.0)
        assert doc["outcomes"][h]["mark_source"] == "webull_last_trade"
    assert doc["status"] == "resolved"
    assert doc["final_verdict"] == "MISSED_WIN"


@pytest.mark.asyncio
async def test_resolver_correct_block_when_direction_wrong(monkeypatch):
    """BUY that would have LOST 100 bps → CORRECT_BLOCK."""
    from shared.learning.outcome_resolver import MarkQuote

    async def _fresh(lane, symbol):
        return MarkQuote(
            price=99.0, source="webull_last_trade",
            ts=_iso(_now()), is_stale=False,
        )

    monkeypatch.setattr(
        "shared.learning.outcome_resolver._fetch_mark_quote", _fresh,
    )

    await db[COUNTERFACTUAL_SIGNALS].insert_one({
        "signal_id": f"{_PFX}corr-block",
        "symbol": "AAPL", "lane": "equity",
        "direction": "BUY",
        "entry_reference_price": 100.0,
        "status": "tracking", "outcomes": {},
        "created_at": _iso(_now() - timedelta(hours=2)),
    })

    await cf.resolve_pending_signals(db)
    doc = await db[COUNTERFACTUAL_SIGNALS].find_one(
        {"signal_id": f"{_PFX}corr-block"},
    )
    assert doc["outcomes"]["5m"]["verdict"] == "CORRECT_BLOCK"
    assert doc["final_verdict"] == "CORRECT_BLOCK"


@pytest.mark.asyncio
async def test_resolver_skips_stale_marks(monkeypatch):
    from shared.learning.outcome_resolver import MarkQuote

    async def _stale(lane, symbol):
        return MarkQuote(
            price=105.0, source="polygon_prev_close",
            ts="2026-02-18T21:00:00+00:00", is_stale=True,
        )

    monkeypatch.setattr(
        "shared.learning.outcome_resolver._fetch_mark_quote", _stale,
    )

    await db[COUNTERFACTUAL_SIGNALS].insert_one({
        "signal_id": f"{_PFX}stale",
        "symbol": "AAPL", "lane": "equity",
        "direction": "BUY",
        "entry_reference_price": 100.0,
        "status": "tracking", "outcomes": {},
        "created_at": _iso(_now() - timedelta(hours=2)),
    })
    counts = await cf.resolve_pending_signals(db)
    assert counts["skipped_stale_mark"] >= 1
    doc = await db[COUNTERFACTUAL_SIGNALS].find_one(
        {"signal_id": f"{_PFX}stale"},
    )
    # No outcomes written on stale.
    assert doc["outcomes"] == {}
    assert doc["status"] == "tracking"
