"""Smoke tests for /app/trader/store.py — the local truth tape.

Doctrine pin (2026-07-01, Path 3): the trader's hot path must NEVER
depend on Mongo. These tests exercise the store in isolation (no
Mongo, no MC lifespan) to prove the JSONL + SQLite pipeline works
end-to-end.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime, timezone

import pytest

sys.path.insert(0, "/app")

from trader import store  # noqa: E402


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@pytest.fixture()
def fresh_store(tmp_path):
    """Fresh SQLite + JSONL dir for every test; asyncio loop up
    because store.init creates an asyncio.Queue."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    sqlite_path = str(tmp_path / "executions.sqlite")
    jsonl_dir = str(tmp_path / "jsonl")
    store.init(sqlite_path, jsonl_dir)
    yield {"sqlite": sqlite_path, "jsonl_dir": jsonl_dir, "loop": loop}
    store.close()
    loop.close()
    asyncio.set_event_loop(None)


def test_record_execution_writes_jsonl_and_sqlite(fresh_store):
    intent_id = "trader-cyc0001-equity"
    store.record_execution({
        "intent_id": intent_id,
        "ts": _iso(),
        "brain": "gto", "lane": "equity", "action": "BUY", "symbol": "TSLA",
        "notional_usd": 9.87,
        "risk_multiplier": 1.0,
        "seats": {"executor": "gto"},
        "angels": {"executor": "Paschar"},
        "risk_ok": True, "risk_reason": "ok",
        "broker": "webull", "broker_order_id": "abc123",
        "broker_status": "submitted",
        "broker_response": {"order_id": "abc123"},
        "ok": True,
    })
    # JSONL exists and holds our row
    jsonl_path = os.path.join(fresh_store["jsonl_dir"], "executions.jsonl")
    assert os.path.exists(jsonl_path)
    with open(jsonl_path) as f:
        rows = [json.loads(line) for line in f]
    assert len(rows) == 1
    assert rows[0]["intent_id"] == intent_id
    # SQLite has it too
    execs = store.recent_executions(limit=10)
    assert len(execs) == 1
    assert execs[0]["intent_id"] == intent_id
    assert execs[0]["ok"] is True
    assert execs[0]["notional_usd"] == pytest.approx(9.87)


def test_already_executed_reflects_ok_rows(fresh_store):
    ok_id = "trader-cyc0002-equity"
    store.record_execution({
        "intent_id": ok_id, "ts": _iso(),
        "notional_usd": 5.0, "ok": True,
    })
    assert store.already_executed(ok_id) is True

    pending_id = "trader-cyc0003-equity"
    store.record_execution({
        "intent_id": pending_id, "ts": _iso(),
        "notional_usd": 5.0, "ok": False,
    })
    # ok=False rows do NOT count as executed — they may retry
    assert store.already_executed(pending_id) is False


def test_daily_spent_usd_sums_today_only(fresh_store):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    yesterday = "2000-01-01"
    store.record_execution({
        "intent_id": "today-1", "ts": f"{today}T12:00:00+00:00",
        "notional_usd": 3.50, "ok": True,
    })
    store.record_execution({
        "intent_id": "today-2", "ts": f"{today}T13:00:00+00:00",
        "notional_usd": 4.25, "ok": True,
    })
    store.record_execution({
        "intent_id": "yesterday-1", "ts": f"{yesterday}T12:00:00+00:00",
        "notional_usd": 999.99, "ok": True,
    })
    store.record_execution({
        "intent_id": "today-not-ok", "ts": f"{today}T14:00:00+00:00",
        "notional_usd": 100.0, "ok": False,
    })
    assert store.daily_spent_usd() == pytest.approx(7.75)


def test_record_receipt_persists_and_reads_back(fresh_store):
    for i in range(3):
        store.record_receipt({
            "cycle_id": f"cyc-{i}",
            "ts": _iso(),
            "lane": "crypto", "symbol": "XBTUSD",
            "last_price": 45000.0 + i,
            "signals": [{"brain": "gto", "verdict": "BUY", "confidence": 0.7}],
            "chosen": {"brain": "gto", "verdict": "BUY"},
            "seats": {"executor": "gto"},
            "angels": {"executor": "Israfel"},
            "risk": {"ok": True, "reason": "ok"},
            "broker_result": None,
            "error": None,
        })
    rows = store.recent_receipts(limit=10)
    assert len(rows) == 3
    assert rows[0]["lane"] == "crypto"
    assert rows[0]["signals"][0]["brain"] == "gto"


def test_seat_cache_upsert_and_read(fresh_store):
    store.upsert_seat_cache("equity:executor", "equity", "executor", "gto", None)
    store.upsert_seat_cache("equity:governor", "equity", "governor", "hellcat", 1.5)
    cached = store.read_seat_cache()
    assert cached["equity:executor"]["holder"] == "gto"
    assert cached["equity:governor"]["risk_multiplier"] == 1.5
    # Upsert overwrites
    store.upsert_seat_cache("equity:executor", "equity", "executor", "camino", None)
    cached2 = store.read_seat_cache()
    assert cached2["equity:executor"]["holder"] == "camino"


def test_flag_cache_roundtrip(fresh_store):
    store.upsert_flag_cache("master_armed", True)
    store.upsert_flag_cache("lane_enabled", {"equity": True, "crypto": False})
    assert store.read_flag_cache("master_armed") is True
    assert store.read_flag_cache("lane_enabled") == {"equity": True, "crypto": False}
    assert store.read_flag_cache("does_not_exist", "fallback") == "fallback"


def test_counts_reports_totals_and_pending(fresh_store):
    store.record_execution({
        "intent_id": "c-1", "ts": _iso(),
        "notional_usd": 1.0, "ok": True,
    })
    store.record_receipt({
        "cycle_id": "cr-1", "ts": _iso(),
        "lane": "equity", "symbol": "TSLA",
        "signals": [], "chosen": None,
        "seats": {}, "angels": {},
        "risk": {},
    })
    c = store.counts()
    assert c["executions_total"] == 1
    assert c["receipts_total"] == 1
    # Nothing has been mirrored yet
    assert c["executions_pending_mongo"] == 1
    assert c["receipts_pending_mongo"] == 1
    # Mirror queue got both items enqueued
    assert c["mirror_queue_size"] == 2


def test_hot_path_does_not_depend_on_mongo(fresh_store):
    """The ultimate proof: with no Mongo of any kind, we can record,
    query, and satisfy every risk-check function locally."""
    # 100 receipts + 20 fills in under a second
    for i in range(100):
        store.record_receipt({
            "cycle_id": f"cyc-{i}", "ts": _iso(),
            "lane": "equity", "symbol": "TSLA",
            "signals": [], "chosen": None, "seats": {}, "angels": {},
            "risk": {"reason": "hold"},
        })
    for i in range(20):
        store.record_execution({
            "intent_id": f"e-{i}", "ts": _iso(),
            "brain": "gto", "lane": "equity", "action": "BUY",
            "symbol": "TSLA", "notional_usd": 1.0, "ok": True,
        })
    assert store.counts()["receipts_total"] == 100
    assert store.counts()["executions_total"] == 20
    assert store.daily_spent_usd() == pytest.approx(20.0)
    assert store.already_executed("e-0") is True
    assert store.already_executed("does-not-exist") is False


def test_prune_removes_old_synced_rows(fresh_store):
    """Rows older than the cutoff AND already mirrored to Mongo are
    deleted. Recent rows and unmirrored rows survive."""
    old = "2020-01-01T00:00:00+00:00"
    fresh = _iso()
    # 3 old-and-mirrored executions
    for i in range(3):
        store.record_execution({
            "intent_id": f"old-mirrored-{i}", "ts": old,
            "notional_usd": 1.0, "ok": True,
        })
    # 2 old-but-not-yet-mirrored
    for i in range(2):
        store.record_execution({
            "intent_id": f"old-pending-{i}", "ts": old,
            "notional_usd": 1.0, "ok": True,
        })
    # 4 fresh rows (whether or not mirrored)
    for i in range(4):
        store.record_execution({
            "intent_id": f"fresh-{i}", "ts": fresh,
            "notional_usd": 1.0, "ok": True,
        })
    # Simulate mirror success on the first 3 old rows + 2 of the fresh
    from trader.store import _mark_synced_exec
    for i in range(3):
        _mark_synced_exec(f"old-mirrored-{i}")
    _mark_synced_exec("fresh-0")
    _mark_synced_exec("fresh-1")

    r = store.prune(days=7, keep_pending=True)
    assert r["executions_deleted"] == 3        # only old + mirrored dropped
    assert r["executions_after"] == 6          # 2 old-pending + 4 fresh survive
    # The unmirrored old rows must still be there (safety default).
    assert store.already_executed("old-pending-0") is True
    assert store.already_executed("fresh-0") is True
    assert store.already_executed("old-mirrored-0") is False


def test_prune_force_deletes_unmirrored(fresh_store):
    """With keep_pending=False, unmirrored old rows are dropped too.
    Operator opt-in only."""
    old = "2020-01-01T00:00:00+00:00"
    for i in range(5):
        store.record_execution({
            "intent_id": f"old-{i}", "ts": old,
            "notional_usd": 1.0, "ok": True,
        })
    r = store.prune(days=7, keep_pending=False)
    assert r["executions_deleted"] == 5
    assert r["executions_after"] == 0


def test_prune_rejects_bad_days(fresh_store):
    with pytest.raises(ValueError):
        store.prune(days=0)
    with pytest.raises(ValueError):
        store.prune(days=-1)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
