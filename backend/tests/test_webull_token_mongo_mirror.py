"""Webull token Mongo mirror — survives-redeploy contract.

Doctrine pin (P1a, 2026-07-04): the token file at
`/app/trader/data/webull_token.json` is ephemeral (pod overlay
filesystem, wiped on redeploy). Mongo mirror survives redeploys.
Removing the token file must NOT lose the token if Mongo has a
copy — the module must transparently rehydrate from Mongo.

These tests exercise the write→disk-wipe→read cycle to prove
`_read_from_disk()` correctly falls back to Mongo when the file
is missing, and rehydrates the disk copy for subsequent reads.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, "/app")
sys.path.insert(0, "/app/backend")


@pytest.fixture()
def isolated_token_state(tmp_path, monkeypatch):
    """Point the token module at a temp file, and give it a
    dedicated Mongo collection so tests don't collide with real
    prod data. Clears any cached in-process state."""
    token_file = tmp_path / "webull_token.json"
    monkeypatch.setenv("WEBULL_TOKEN_PATH", str(token_file))

    # Use the real MONGO_URL/DB_NAME but a test-scoped collection
    # name so we don't touch the prod `webull_token` collection.
    import trader.webull_auth as wa
    original_name = wa._MONGO_COLL_NAME
    monkeypatch.setattr(wa, "_MONGO_COLL_NAME", "webull_token_test")

    # Clear any cached token from a prior test.
    wa._cache = None

    # Wipe the test collection at start + end.
    coll = wa._mongo_collection()
    if coll is not None:
        coll.delete_many({})

    yield token_file, wa

    # cleanup
    wa._cache = None
    if coll is not None:
        coll.delete_many({})


def test_write_to_disk_also_mirrors_to_mongo(isolated_token_state):
    """Every _write_to_disk call must also write to Mongo so that
    a subsequent redeploy can restore from Mongo."""
    token_file, wa = isolated_token_state
    payload = {
        "token": "test-token-abc123",
        "expires": 1783200000000,
        "status": "PENDING",
        "created_at": "2026-07-04T00:00:00+00:00",
        "base": "https://api.webull.com",
    }
    wa._write_to_disk(payload)

    # Disk copy exists
    assert token_file.exists()
    assert json.loads(token_file.read_text())["token"] == "test-token-abc123"

    # Mongo mirror exists
    mongo_payload = wa._read_from_mongo()
    assert mongo_payload is not None
    assert mongo_payload["token"] == "test-token-abc123"
    assert mongo_payload["status"] == "PENDING"


def test_read_from_disk_restores_from_mongo_when_file_missing(isolated_token_state):
    """Simulate the post-redeploy state: Mongo has the token but
    the disk file was wiped. _read_from_disk() must fall back to
    Mongo AND rehydrate the disk copy."""
    token_file, wa = isolated_token_state
    payload = {
        "token": "restored-token-xyz789",
        "expires": 1783200000000,
        "status": "NORMAL",
        "created_at": "2026-07-04T00:00:00+00:00",
        "base": "https://api.webull.com",
    }
    # Write to both (simulating a token that was created pre-redeploy)
    wa._write_to_disk(payload)
    assert token_file.exists()

    # Simulate redeploy: wipe the disk file
    token_file.unlink()
    assert not token_file.exists()
    wa._cache = None  # also wipe the in-process cache

    # Read must transparently restore from Mongo
    result = wa._read_from_disk()
    assert result is not None
    assert result["token"] == "restored-token-xyz789"
    assert result["status"] == "NORMAL"

    # And rehydrate the disk copy for subsequent reads
    assert token_file.exists()
    assert json.loads(token_file.read_text())["token"] == "restored-token-xyz789"


def test_read_returns_none_when_neither_disk_nor_mongo_has_token(isolated_token_state):
    """Fresh install / no token ever created: both paths empty."""
    token_file, wa = isolated_token_state
    # Ensure both empty
    assert not token_file.exists()
    assert wa._read_from_mongo() is None
    # Result should be None, not raise
    assert wa._read_from_disk() is None


def test_get_token_uses_mongo_restore_path(isolated_token_state):
    """End-to-end contract: get_token() must return the restored
    token after a simulated redeploy."""
    token_file, wa = isolated_token_state
    payload = {"token": "e2e-token", "expires": 0, "status": "NORMAL"}
    wa._write_to_disk(payload)
    # Simulate redeploy
    token_file.unlink()
    wa._cache = None
    # get_token() → _read_from_disk() → falls to Mongo → returns token
    assert wa.get_token() == "e2e-token"


def test_write_is_idempotent_upsert_not_insert(isolated_token_state):
    """A second write must REPLACE the Mongo doc, not create a
    duplicate. Otherwise a token refresh over the 15-day cycle
    would accumulate stale docs."""
    token_file, wa = isolated_token_state
    wa._write_to_disk({"token": "v1", "expires": 0, "status": "PENDING"})
    wa._write_to_disk({"token": "v2", "expires": 0, "status": "NORMAL"})

    coll = wa._mongo_collection()
    assert coll is not None
    count = coll.count_documents({})
    assert count == 1  # not 2

    # And the latest value wins
    doc = coll.find_one({"_id": wa._MONGO_DOC_ID})
    assert doc["token"] == "v2"
    assert doc["status"] == "NORMAL"


def test_mongo_unreachable_does_not_break_disk_write(isolated_token_state, monkeypatch):
    """Doctrine: Mongo mirror is BEST-EFFORT. If Mongo is down,
    the disk write still succeeds and current-pod operation
    continues. Pre-fix behavior at worst — not a regression."""
    token_file, wa = isolated_token_state
    # Force the mongo path to fail
    monkeypatch.setattr(wa, "_mongo_collection", lambda: None)
    payload = {"token": "no-mongo", "expires": 0, "status": "PENDING"}
    # Must not raise
    wa._write_to_disk(payload)
    # Disk still got written
    assert token_file.exists()
    assert json.loads(token_file.read_text())["token"] == "no-mongo"


# ═══════════════════════════════════════════════════════════════════
# Fresher-tier-wins (2026-02-19 operator directive)
# ═══════════════════════════════════════════════════════════════════
#
# Prior contract was disk-FIRST: if the disk file existed, it was
# used and Mongo was consulted only as a fallback when disk was
# empty. That created a stale-disk problem on production — a
# committed repo can carry an old `webull_token.json` and every
# redeploy resurrects the stale copy over the fresh Mongo mirror.
#
# New contract: whichever tier has the newer `created_at` wins.
# If Mongo wins, disk is rehydrated so future reads are fast.


def test_read_prefers_mongo_when_mongo_is_newer(isolated_token_state):
    """Disk has an OLD copy, Mongo has a NEW copy → Mongo wins."""
    token_file, wa = isolated_token_state

    # 1. Simulate the committed-repo stale disk state (July 1).
    stale_disk = {
        "token": "stale-disk-token",
        "expires": 1782925758272,
        "status": "PENDING",
        "created_at": "2026-07-01T17:03:18+00:00",
        "base": "https://api.webull.com",
    }
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(json.dumps(stale_disk, indent=2))

    # 2. Simulate a fresher Mongo mirror (July 8, 7 days later).
    fresh_mongo = {
        "token": "fresh-mongo-token",
        "expires": 1784799833339,
        "status": "PENDING",
        "created_at": "2026-07-08T09:43:53+00:00",
        "base": "https://api.webull.com",
    }
    coll = wa._mongo_collection()
    assert coll is not None
    coll.replace_one(
        {"_id": wa._MONGO_DOC_ID},
        {**fresh_mongo, "_id": wa._MONGO_DOC_ID},
        upsert=True,
    )
    wa._cache = None

    result = wa._read_from_disk()
    assert result is not None
    assert result["token"] == "fresh-mongo-token", (
        "Stale-disk beat fresh-Mongo — freshness contract broken"
    )
    # Disk must have been rehydrated with the fresh Mongo payload.
    assert json.loads(token_file.read_text())["token"] == "fresh-mongo-token"


def test_read_prefers_disk_when_disk_is_newer(isolated_token_state):
    """Disk has a NEW copy (just created), Mongo has an OLD copy
    (mirror hasn't caught up yet) → disk wins. This is the
    same-pod steady-state case where a token refresh landed on
    disk before the Mongo write completed."""
    token_file, wa = isolated_token_state

    old_mongo = {
        "token": "old-mongo",
        "expires": 0,
        "status": "PENDING",
        "created_at": "2026-06-01T00:00:00+00:00",
    }
    coll = wa._mongo_collection()
    coll.replace_one(
        {"_id": wa._MONGO_DOC_ID},
        {**old_mongo, "_id": wa._MONGO_DOC_ID},
        upsert=True,
    )
    fresh_disk = {
        "token": "fresh-disk",
        "expires": 0,
        "status": "PENDING",
        "created_at": "2026-07-15T00:00:00+00:00",
    }
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(json.dumps(fresh_disk, indent=2))
    wa._cache = None

    result = wa._read_from_disk()
    assert result is not None
    assert result["token"] == "fresh-disk"


def test_read_falls_back_to_only_available_tier(isolated_token_state):
    """Only Mongo has a copy (no disk file) → Mongo wins.
    Only disk has a copy (no Mongo doc) → disk wins.
    This is a sanity check that the freshness logic doesn't
    accidentally reject the sole surviving tier."""
    token_file, wa = isolated_token_state

    # Case A: only Mongo has a copy.
    only_mongo = {
        "token": "only-mongo",
        "created_at": "2026-07-01T00:00:00+00:00",
    }
    coll = wa._mongo_collection()
    coll.replace_one(
        {"_id": wa._MONGO_DOC_ID},
        {**only_mongo, "_id": wa._MONGO_DOC_ID},
        upsert=True,
    )
    if token_file.exists():
        token_file.unlink()
    wa._cache = None
    result = wa._read_from_disk()
    assert result is not None
    assert result["token"] == "only-mongo"

    # Case B: only disk has a copy.
    coll.delete_many({})
    only_disk = {
        "token": "only-disk",
        "created_at": "2026-07-02T00:00:00+00:00",
    }
    token_file.write_text(json.dumps(only_disk, indent=2))
    wa._cache = None
    result = wa._read_from_disk()
    assert result is not None
    assert result["token"] == "only-disk"


def test_read_handles_missing_created_at_gracefully(isolated_token_state):
    """A payload with no `created_at` (legacy schema) MUST NOT crash
    the reader. Ordering falls back to whichever payload has a
    non-empty ts; if both lack it, disk wins as a stable default."""
    token_file, wa = isolated_token_state

    disk_no_ts = {"token": "disk-legacy", "status": "PENDING"}
    mongo_no_ts = {"token": "mongo-legacy", "status": "PENDING"}
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(json.dumps(disk_no_ts, indent=2))
    coll = wa._mongo_collection()
    coll.replace_one(
        {"_id": wa._MONGO_DOC_ID},
        {**mongo_no_ts, "_id": wa._MONGO_DOC_ID},
        upsert=True,
    )
    wa._cache = None

    result = wa._read_from_disk()
    assert result is not None
    # Neither has a ts — disk wins by tie-break (deterministic).
    assert result["token"] == "disk-legacy"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
