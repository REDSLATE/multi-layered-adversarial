"""Seats reverse-sync — recovery-tool safety contract (2026-02-17).

Locks in the operator-pinned doctrine:

    seat_registry            = source of truth (READ-only)
    brain_roster             = repaired mirror (WRITE target)
    NEVER delete registry rows
    return before/after diff
    audit-log every write
    REFUSE if seat_registry has missing / duplicate canonical seats
    (running the sync with a corrupt registry would compound damage —
     the guard turns this from an accidental wipe button into a
     recovery tool)
"""
from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, "/app")
sys.path.insert(0, "/app/backend")


# ─── Fake DB scaffolding ────────────────────────────────────────────

def _make_db(registry_rows: list[dict], roster_doc: dict | None = None):
    """Motor-style stub. `registry_rows` supplies `seat_registry.find({})`,
    `roster_doc` supplies `brain_roster.find_one({"_id": "current"})`.
    Records all mutations to `_writes`."""
    writes: list[dict] = []
    audits: list[dict] = []
    deletions: list[dict] = []  # tracks calls to registry delete — must stay empty

    def _reg_find(query=None, projection=None):  # noqa: ARG001
        class _Cur:
            def __init__(self, data): self._data = list(data)
            def __aiter__(self):
                self._i = 0
                return self
            async def __anext__(self):
                if self._i >= len(self._data):
                    raise StopAsyncIteration
                r = self._data[self._i]
                self._i += 1
                return r
        return _Cur(registry_rows)

    reg = MagicMock()
    reg.find = _reg_find
    async def _reg_delete_one(*a, **k):
        deletions.append({"args": a, "kwargs": k})
        return MagicMock(deleted_count=1)
    async def _reg_delete_many(*a, **k):
        deletions.append({"args": a, "kwargs": k, "many": True})
        return MagicMock(deleted_count=1)
    reg.delete_one = _reg_delete_one
    reg.delete_many = _reg_delete_many

    roster = MagicMock()
    async def _roster_find_one(query=None, projection=None):  # noqa: ARG001
        return roster_doc
    roster.find_one = _roster_find_one
    async def _roster_update_one(query, update, upsert=False):  # noqa: ARG001
        writes.append({"collection": "brain_roster", "query": query,
                       "update": update, "upsert": upsert})
    roster.update_one = _roster_update_one

    audit = MagicMock()
    async def _audit_insert_one(doc):
        audits.append(doc)
    audit.insert_one = _audit_insert_one

    fake_db = MagicMock()
    def _getitem(name):
        return {
            "seat_registry": reg,
            "brain_roster":  roster,
            "roster_audit_log": audit,
        }[name]
    fake_db.__getitem__ = MagicMock(side_effect=_getitem)
    return fake_db, writes, audits, deletions


_ALL_SEATS_CLEAN = [
    {"_id": "equity:strategist", "holder": "barracuda"},
    {"_id": "equity:governor",   "holder": "hellcat"},
    {"_id": "equity:executor",   "holder": "camino"},
    {"_id": "equity:auditor",    "holder": "gto"},
    {"_id": "crypto:strategist", "holder": "camino"},
    {"_id": "crypto:governor",   "holder": "hellcat"},
    {"_id": "crypto:executor",   "holder": "gto"},
    {"_id": "crypto:auditor",    "holder": "barracuda"},
]

_USER = {"email": "admin@risedual.io"}


# ─── Happy path ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reverse_sync_writes_all_8_seats_from_clean_registry():
    from routes import seats_reverse_sync as m
    from routes.seats_reverse_sync import ReverseSyncIn

    fake_db, writes, audits, deletions = _make_db(
        _ALL_SEATS_CLEAN,
        roster_doc={"_id": "current", "assignments": {}},
    )

    with patch.object(m, "db", fake_db):
        out = await m.reverse_sync_from_registry(ReverseSyncIn(dry_run=False), user=_USER)

    assert out["ok"] is True
    assert out["dry_run"] is False
    assert out["writes_applied"] == 1
    # All 8 canonical keys must appear in the after-assignments:
    expected_keys = {"strategist", "governor", "executor", "auditor",
                     "crypto_strategist", "crypto_governor", "crypto",
                     "crypto_auditor"}
    assert set(out["after"].keys()) == expected_keys
    assert out["after"]["crypto"] == "gto", (
        "Canonical crypto executor key must be 'crypto' (NOT 'crypto_executor'). "
        f"Got: {out['after']}"
    )
    # Registry rows MUST NOT be deleted:
    assert not deletions, f"Registry rows were touched: {deletions}"


@pytest.mark.asyncio
async def test_reverse_sync_returns_before_after_diff():
    from routes import seats_reverse_sync as m
    from routes.seats_reverse_sync import ReverseSyncIn

    prior = {"strategist": "gto", "executor": "camino"}  # partial, stale roster
    fake_db, writes, audits, _ = _make_db(
        _ALL_SEATS_CLEAN,
        roster_doc={"_id": "current", "assignments": prior},
    )

    with patch.object(m, "db", fake_db):
        out = await m.reverse_sync_from_registry(ReverseSyncIn(dry_run=False), user=_USER)

    # Diff should reflect every non-matching key. Prior had strategist=gto
    # but registry says barracuda → must be in diff.
    strategist_diff = [d for d in out["diff"] if d["key"] == "strategist"]
    assert strategist_diff == [{"key": "strategist", "before": "gto", "after": "barracuda"}]
    # `executor` was already correct → NOT in diff.
    assert not [d for d in out["diff"] if d["key"] == "executor"]
    # All 6 other keys were missing in prior → all should be in diff.
    assert len(out["diff"]) == 7  # 6 missing + 1 changed


# ─── Guards: missing / extra canonical seats ────────────────────────


@pytest.mark.asyncio
async def test_reverse_sync_refuses_when_missing_canonical_seat():
    """The operator doctrine: refuse if the registry is INCOMPLETE.
    A missing seat means the source of truth is itself corrupt —
    reverse-syncing corrupt data would compound the damage."""
    from routes import seats_reverse_sync as m
    from routes.seats_reverse_sync import ReverseSyncIn
    from fastapi import HTTPException

    # Missing crypto:auditor
    partial = [r for r in _ALL_SEATS_CLEAN if r["_id"] != "crypto:auditor"]
    fake_db, writes, _, _ = _make_db(partial, roster_doc={"_id": "current", "assignments": {}})

    with patch.object(m, "db", fake_db):
        with pytest.raises(HTTPException) as excinfo:
            await m.reverse_sync_from_registry(ReverseSyncIn(dry_run=False), user=_USER)

    assert excinfo.value.status_code == 409
    assert "crypto:auditor" in str(excinfo.value.detail)
    assert "INCOMPLETE" in str(excinfo.value.detail)
    # NO write happened:
    assert not writes


@pytest.mark.asyncio
async def test_reverse_sync_refuses_when_registry_completely_empty():
    """Extreme case: registry has zero rows. All 8 canonical seats
    missing → refuse."""
    from routes import seats_reverse_sync as m
    from routes.seats_reverse_sync import ReverseSyncIn
    from fastapi import HTTPException

    fake_db, writes, _, _ = _make_db([], roster_doc={"_id": "current", "assignments": {}})

    with patch.object(m, "db", fake_db):
        with pytest.raises(HTTPException) as excinfo:
            await m.reverse_sync_from_registry(ReverseSyncIn(dry_run=False), user=_USER)

    assert excinfo.value.status_code == 409
    assert not writes


@pytest.mark.asyncio
async def test_reverse_sync_tolerates_extra_non_canonical_rows():
    """Extras beyond the 8 canonical seats don't imperil correctness —
    they simply don't map. The endpoint should proceed, ignoring them."""
    from routes import seats_reverse_sync as m
    from routes.seats_reverse_sync import ReverseSyncIn

    with_extra = _ALL_SEATS_CLEAN + [
        {"_id": "options:strategist", "holder": "someone"},  # nonsensical lane
    ]
    fake_db, writes, _, _ = _make_db(with_extra,
                                      roster_doc={"_id": "current", "assignments": {}})

    with patch.object(m, "db", fake_db):
        out = await m.reverse_sync_from_registry(ReverseSyncIn(dry_run=False), user=_USER)

    assert out["ok"] is True
    assert out["writes_applied"] == 1
    # The extra key should NOT appear in the projected assignments.
    assert "options" not in out["after"]
    assert "options_strategist" not in out["after"]


# ─── Never-delete-registry contract ─────────────────────────────────


@pytest.mark.asyncio
async def test_reverse_sync_never_calls_registry_delete():
    """Contract: this endpoint MUST be read-only against seat_registry.
    Delete calls of any kind (delete_one, delete_many, drop) are a
    doctrine violation."""
    from routes import seats_reverse_sync as m
    from routes.seats_reverse_sync import ReverseSyncIn

    fake_db, _, _, deletions = _make_db(_ALL_SEATS_CLEAN, roster_doc=None)
    with patch.object(m, "db", fake_db):
        await m.reverse_sync_from_registry(ReverseSyncIn(dry_run=False), user=_USER)
    assert not deletions, (
        "seat_registry MUST be treated as read-only. Deletions: "
        + str(deletions)
    )


@pytest.mark.asyncio
async def test_dry_run_makes_no_writes_to_brain_roster():
    """dry_run=True must return the diff without touching brain_roster
    OR incrementing seat_epoch."""
    from routes import seats_reverse_sync as m
    from routes.seats_reverse_sync import ReverseSyncIn

    fake_db, writes, audits, _ = _make_db(_ALL_SEATS_CLEAN,
                                          roster_doc={"_id": "current", "assignments": {}})
    with patch.object(m, "db", fake_db):
        out = await m.reverse_sync_from_registry(ReverseSyncIn(dry_run=True), user=_USER)
    assert out["dry_run"] is True
    assert out["writes_applied"] == 0
    assert not writes, "dry_run must not write to brain_roster"
    # Audit trail still records the dry-run — you want a record of every
    # time someone even THOUGHT about reverse-syncing.
    assert len(audits) == 1
    assert audits[0]["dry_run"] is True


# ─── Audit-log-every-write contract ─────────────────────────────────


@pytest.mark.asyncio
async def test_every_apply_writes_an_audit_row():
    """Every write path (dry_run OR apply) MUST append to roster_audit_log."""
    from routes import seats_reverse_sync as m
    from routes.seats_reverse_sync import ReverseSyncIn

    fake_db, writes, audits, _ = _make_db(_ALL_SEATS_CLEAN,
                                          roster_doc={"_id": "current", "assignments": {}})
    with patch.object(m, "db", fake_db):
        await m.reverse_sync_from_registry(ReverseSyncIn(dry_run=False), user=_USER)
    assert len(audits) == 1
    row = audits[0]
    assert row["event"] == "reverse_sync_from_registry"
    assert row["actor"] == "admin@risedual.io"
    assert "before_assignments" in row
    assert "after_assignments" in row
    assert "diff" in row
    assert "ts" in row


# ─── Canonical crypto executor key (the 2026-06-18 migration guard) ─


@pytest.mark.asyncio
async def test_crypto_executor_stored_under_canonical_crypto_key():
    """The crypto executor holder from `seat_registry` (row
    `crypto:executor`) MUST land under `brain_roster.assignments.crypto`
    — NOT `crypto_executor`. Per the 2026-06-18 roster migration and
    the 2026-02-17 seat-drift fix, `crypto` is the canonical key."""
    from routes import seats_reverse_sync as m
    from routes.seats_reverse_sync import ReverseSyncIn

    fake_db, writes, _, _ = _make_db(_ALL_SEATS_CLEAN,
                                      roster_doc={"_id": "current", "assignments": {}})
    with patch.object(m, "db", fake_db):
        out = await m.reverse_sync_from_registry(ReverseSyncIn(dry_run=False), user=_USER)

    assert out["after"]["crypto"] == "gto"
    assert "crypto_executor" not in out["after"], (
        "crypto_executor is the DEAD LEGACY ALIAS. Reverse-sync must "
        "emit the canonical `crypto` key, not the alias."
    )
