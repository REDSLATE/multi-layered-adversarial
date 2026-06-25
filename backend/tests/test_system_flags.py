"""System Flags — DB-backed runtime toggles.

Operator pin (2026-02-23): replaces env-var-only flow for
PARADOX_V3_BRAINS / PARADOX_V3_TRIGGER_WATCHER / PARADOX_V3_TRIGGER_
REFIRE. Tests cover:

  * `set_paradox_v3_brains` upserts the row + writes an audit row
  * Sync `effective_paradox_v3_brains()` reads DB first, env fallback
  * Empty list explicitly means "no brains" (DB wins even with env set)
  * `null` DB value falls back to env behaviour
  * Trigger watcher / refire toggles behave the same way
  * The audit feed surfaces recent flips in reverse chronological order
  * `v3_brain_enabled` (sync) honours the DB cache
  * Idempotent set re-running is safe (one row, history append)
"""
from __future__ import annotations

import os

import pytest

from db import db
from namespaces import SYSTEM_FLAGS, SYSTEM_FLAG_CHANGES
from shared.system_flags import (
    effective_paradox_v3_brains,
    effective_trigger_refire_enabled,
    effective_trigger_watcher_enabled,
    recent_flag_changes,
    refresh_system_flags,
    set_paradox_v3_brains,
    set_trigger_refire,
    set_trigger_watcher,
)


pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _isolate_system_flags():
    """Reset DB + cache BEFORE and AFTER each test so a stray flag
    can't pollute legacy v3 tests (test_paradox_v3_step4 et al)."""
    await db[SYSTEM_FLAGS].delete_many({})
    await db[SYSTEM_FLAG_CHANGES].delete_many({})
    await refresh_system_flags()
    yield
    await db[SYSTEM_FLAGS].delete_many({})
    await db[SYSTEM_FLAG_CHANGES].delete_many({})
    await refresh_system_flags()


async def _reset():
    await db[SYSTEM_FLAGS].delete_many({})
    await db[SYSTEM_FLAG_CHANGES].delete_many({})
    await refresh_system_flags()


async def test_set_paradox_v3_brains_upserts_and_audits():
    await _reset()
    snap = await set_paradox_v3_brains(["camino"], actor="admin@x")
    assert snap.paradox_v3_brains == ["camino"]

    row = await db[SYSTEM_FLAGS].find_one({"_id": "current"})
    assert row is not None
    assert row["paradox_v3_brains"] == ["camino"]
    assert row["updated_by"] == "admin@x"

    audit = await db[SYSTEM_FLAG_CHANGES].find_one({"flag": "paradox_v3_brains"})
    assert audit is not None
    assert audit["before"] is None
    assert audit["after"] == ["camino"]
    assert audit["actor"] == "admin@x"


async def test_effective_paradox_v3_brains_db_wins_over_env(monkeypatch):
    await _reset()
    monkeypatch.setenv("PARADOX_V3_BRAINS", "barracuda,hellcat")
    # Cache empty → env fallback.
    assert set(effective_paradox_v3_brains()) == {"barracuda", "hellcat"}
    # Set DB → DB wins.
    await set_paradox_v3_brains(["camino"], actor="t")
    assert effective_paradox_v3_brains() == ["camino"]


async def test_empty_list_means_explicit_none_not_env_fallback(monkeypatch):
    await _reset()
    monkeypatch.setenv("PARADOX_V3_BRAINS", "camino")
    # DB cache empty → env wins → camino.
    assert effective_paradox_v3_brains() == ["camino"]
    # Operator explicitly sets to []. DB wins → no brains.
    await set_paradox_v3_brains([], actor="t")
    assert effective_paradox_v3_brains() == []


async def test_null_db_falls_back_to_env(monkeypatch):
    """After reset, the row doesn't exist → snapshot.paradox_v3_brains
    is None → env fallback applies."""
    await _reset()
    monkeypatch.setenv("PARADOX_V3_BRAINS", "gto")
    assert effective_paradox_v3_brains() == ["gto"]


async def test_trigger_watcher_toggle_round_trip(monkeypatch):
    await _reset()
    monkeypatch.setenv("PARADOX_V3_TRIGGER_WATCHER", "0")
    assert effective_trigger_watcher_enabled() is False
    await set_trigger_watcher(True, actor="t")
    assert effective_trigger_watcher_enabled() is True
    await set_trigger_watcher(False, actor="t")
    assert effective_trigger_watcher_enabled() is False
    # 3 audit rows total (paradox_v3_brains may have rows from earlier
    # tests; filter just to the watcher flag).
    cnt = await db[SYSTEM_FLAG_CHANGES].count_documents(
        {"flag": "trigger_watcher_enabled"},
    )
    assert cnt == 2  # True then False


async def test_trigger_refire_toggle_round_trip(monkeypatch):
    await _reset()
    monkeypatch.setenv("PARADOX_V3_TRIGGER_REFIRE", "0")
    assert effective_trigger_refire_enabled() is False
    await set_trigger_refire(True, actor="t")
    assert effective_trigger_refire_enabled() is True


async def test_v3_brain_enabled_sync_honours_db_cache():
    """The sync `v3_brain_enabled` helper used by the brain runner
    must reflect DB state without an env reload."""
    from shared.intent_envelope_v3 import v3_brain_enabled
    await _reset()
    # Ensure env doesn't accidentally satisfy the check.
    os.environ.pop("PARADOX_V3_BRAINS", None)
    assert v3_brain_enabled("camino") is False
    await set_paradox_v3_brains(["camino"], actor="t")
    assert v3_brain_enabled("camino") is True
    assert v3_brain_enabled("barracuda") is False


async def test_watcher_refire_sync_helpers_honour_db_cache():
    from shared.pipeline.trigger_watcher import (
        is_refire_enabled,
        is_watcher_enabled,
    )
    await _reset()
    os.environ.pop("PARADOX_V3_TRIGGER_WATCHER", None)
    os.environ.pop("PARADOX_V3_TRIGGER_REFIRE", None)
    assert is_watcher_enabled() is False
    assert is_refire_enabled() is False
    await set_trigger_watcher(True, actor="t")
    await set_trigger_refire(True, actor="t")
    assert is_watcher_enabled() is True
    assert is_refire_enabled() is True


async def test_recent_flag_changes_reverse_chronological():
    await _reset()
    await set_paradox_v3_brains(["camino"], actor="t")
    await set_paradox_v3_brains(["camino", "gto"], actor="t")
    await set_paradox_v3_brains(["gto"], actor="t")
    rows = await recent_flag_changes(limit=5)
    # Newest first.
    assert rows[0]["after"] == ["gto"]
    assert rows[1]["after"] == ["camino", "gto"]
    assert rows[2]["after"] == ["camino"]


async def test_idempotent_repeat_set_still_audits():
    """Setting the same value twice writes 2 audit rows but only one
    `current` doc — operator may want a "no-op flip" entry for the
    historical record, so we don't dedupe."""
    await _reset()
    await set_paradox_v3_brains(["camino"], actor="t")
    await set_paradox_v3_brains(["camino"], actor="t")
    count_current = await db[SYSTEM_FLAGS].count_documents({"_id": "current"})
    assert count_current == 1
    audit = await db[SYSTEM_FLAG_CHANGES].count_documents(
        {"flag": "paradox_v3_brains"},
    )
    assert audit == 2
