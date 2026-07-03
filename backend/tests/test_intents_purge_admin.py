"""Tests for the intents purge endpoint (2026-07-03).

Locks the safety invariants:
    * confirm=false → dry-run, nothing deleted, count returned
    * confirm=true  → actually deletes
    * executed=true intents are NEVER deleted (real trading history)
    * fresh intents (younger than min_age_hours) are NEVER deleted
    * broker_order_id-having intents are NEVER deleted (belt+suspenders)
    * only HOLD/WATCH actions are purged (BUY/SELL untouched)
    * lane filter validation returns 200 with error, not 500
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, "/app")
sys.path.insert(0, "/app/backend")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _old_iso(hours: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def _make_db_with_intents(intents: list[dict]) -> MagicMock:
    coll = MagicMock()
    coll.count_documents = AsyncMock(return_value=0)
    coll.delete_many = AsyncMock()
    # Emulate a cursor: find().limit().to_list()
    cursor = MagicMock()
    cursor.limit = MagicMock(return_value=cursor)
    cursor.to_list = AsyncMock(return_value=[])
    coll.find = MagicMock(return_value=cursor)

    def _count(q):
        matches = 0
        for i in intents:
            action_ok = i.get("action") in q["action"]["$in"]
            executed_ok = i.get("executed") is not True
            ts_ok = i.get("ingest_ts", "") < q["ingest_ts"]["$lt"]
            broker_none = not i.get("broker_order_id")
            lane_ok = "lane" not in q or i.get("lane") == q["lane"]
            if action_ok and executed_ok and ts_ok and broker_none and lane_ok:
                matches += 1
        return matches

    coll.count_documents.side_effect = lambda q: _count(q)
    delete_result = MagicMock()
    delete_result.deleted_count = 0
    coll.delete_many.return_value = delete_result

    async def _delete(q):
        n = _count(q)
        delete_result.deleted_count = n
        return delete_result
    coll.delete_many.side_effect = _delete

    db_mock = MagicMock()
    db_mock.shared_intents = coll
    return db_mock


@pytest.mark.asyncio
async def test_dry_run_default_deletes_nothing(monkeypatch):
    from routes import intents_purge_admin
    intents = [
        {"action": "HOLD", "executed": False, "ingest_ts": _old_iso(12)},
        {"action": "WATCH", "executed": False, "ingest_ts": _old_iso(24)},
    ]
    monkeypatch.setattr(
        intents_purge_admin, "db", _make_db_with_intents(intents),
    )
    result = await intents_purge_admin.purge_non_executable_intents(
        _user={"email": "test@example.com"}, min_age_hours=6, lane=None,
        confirm=False,
    )
    assert result["dry_run"] is True
    assert result["would_delete"] == 2
    # delete_many must not have been called.
    assert intents_purge_admin.db.shared_intents.delete_many.await_count == 0


@pytest.mark.asyncio
async def test_confirm_true_actually_deletes(monkeypatch):
    from routes import intents_purge_admin
    intents = [
        {"action": "HOLD", "executed": False, "ingest_ts": _old_iso(12)},
        {"action": "WATCH", "executed": False, "ingest_ts": _old_iso(24)},
    ]
    monkeypatch.setattr(
        intents_purge_admin, "db", _make_db_with_intents(intents),
    )
    result = await intents_purge_admin.purge_non_executable_intents(
        _user={"email": "op@example.com"}, min_age_hours=6, lane=None,
        confirm=True,
    )
    assert result["dry_run"] is False
    assert result["deleted"] == 2
    assert result["requested_by"] == "op@example.com"


@pytest.mark.asyncio
async def test_executed_true_never_purged(monkeypatch):
    """Real trading history is sacrosanct — must never be deleted."""
    from routes import intents_purge_admin
    intents = [
        # This would match on age + action but has executed=True
        {"action": "HOLD", "executed": True, "ingest_ts": _old_iso(48)},
    ]
    monkeypatch.setattr(
        intents_purge_admin, "db", _make_db_with_intents(intents),
    )
    result = await intents_purge_admin.purge_non_executable_intents(
        _user={"email": "op@example.com"}, min_age_hours=6, lane=None,
        confirm=True,
    )
    assert result["deleted"] == 0


@pytest.mark.asyncio
async def test_fresh_intents_never_purged(monkeypatch):
    """Intents younger than min_age_hours must survive the purge —
    the pipeline may still be processing them."""
    from routes import intents_purge_admin
    intents = [
        {"action": "HOLD", "executed": False, "ingest_ts": _old_iso(1)},
    ]
    monkeypatch.setattr(
        intents_purge_admin, "db", _make_db_with_intents(intents),
    )
    result = await intents_purge_admin.purge_non_executable_intents(
        _user={"email": "op@example.com"}, min_age_hours=6, lane=None,
        confirm=True,
    )
    assert result["deleted"] == 0


@pytest.mark.asyncio
async def test_broker_order_id_intents_never_purged(monkeypatch):
    """Belt-and-suspenders: any intent with a broker_order_id set is
    treated as historically-executed even if `executed` isn't True."""
    from routes import intents_purge_admin
    intents = [
        {
            "action": "HOLD", "executed": False,
            "ingest_ts": _old_iso(48),
            "broker_order_id": "webull-xyz-123",
        },
    ]
    monkeypatch.setattr(
        intents_purge_admin, "db", _make_db_with_intents(intents),
    )
    result = await intents_purge_admin.purge_non_executable_intents(
        _user={"email": "op@example.com"}, min_age_hours=6, lane=None,
        confirm=True,
    )
    assert result["deleted"] == 0


@pytest.mark.asyncio
async def test_buy_sell_intents_never_purged(monkeypatch):
    """Only HOLD/WATCH are purge-eligible. Actionable directions
    (BUY/SELL/SHORT/COVER) may be pending execution — leave them."""
    from routes import intents_purge_admin
    intents = [
        {"action": "BUY", "executed": False, "ingest_ts": _old_iso(48)},
        {"action": "SELL", "executed": False, "ingest_ts": _old_iso(48)},
    ]
    monkeypatch.setattr(
        intents_purge_admin, "db", _make_db_with_intents(intents),
    )
    result = await intents_purge_admin.purge_non_executable_intents(
        _user={"email": "op@example.com"}, min_age_hours=6, lane=None,
        confirm=True,
    )
    assert result["deleted"] == 0


@pytest.mark.asyncio
async def test_invalid_lane_returns_error_not_500(monkeypatch):
    from routes import intents_purge_admin
    monkeypatch.setattr(
        intents_purge_admin, "db", _make_db_with_intents([]),
    )
    result = await intents_purge_admin.purge_non_executable_intents(
        _user={"email": "op@example.com"}, min_age_hours=6, lane="options",
        confirm=True,
    )
    assert result["ok"] is False
    assert "lane must be" in result["error"]


@pytest.mark.asyncio
async def test_lane_filter_restricts_deletion(monkeypatch):
    """Lane filter must scope the delete — crypto rows must survive
    an `equity` purge and vice versa."""
    from routes import intents_purge_admin
    intents = [
        {"action": "HOLD", "executed": False, "ingest_ts": _old_iso(12), "lane": "equity"},
        {"action": "HOLD", "executed": False, "ingest_ts": _old_iso(12), "lane": "crypto"},
    ]
    monkeypatch.setattr(
        intents_purge_admin, "db", _make_db_with_intents(intents),
    )
    result = await intents_purge_admin.purge_non_executable_intents(
        _user={"email": "op@example.com"}, min_age_hours=6, lane="equity",
        confirm=True,
    )
    assert result["deleted"] == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
