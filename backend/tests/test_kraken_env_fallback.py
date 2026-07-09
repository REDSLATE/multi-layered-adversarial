"""Env-var fallback tests for `shared.crypto.kraken.get_active_keys_status`.

Doctrine (2026-07-09 iter-22, operator directive):

    MC's Kraken adapter historically read the encrypted Mongo
    `kraken_credentials._id="singleton"` doc only. Production's
    Kraken keys were set as `KRAKEN_API_KEY` / `KRAKEN_API_SECRET`
    env vars (the trader-sidecar pattern) and were never migrated
    into the singleton — so MC couldn't submit Kraken orders even
    though the sidecar could.

    Fix: when the Mongo singleton is missing/malformed/undecryptable,
    fall back to the env vars and log a WARNING so the operator
    sees the temporary bridge in play. This suite locks that
    fallback in.
"""
from __future__ import annotations

import sys

import pytest

sys.path.insert(0, "/app/backend")

from db import db  # noqa: E402
from namespaces import KRAKEN_CREDENTIALS  # noqa: E402
from shared.crypto.kraken import get_active_keys_status  # noqa: E402


@pytest.fixture(autouse=True)
async def _preserve_singleton(monkeypatch):
    """Snapshot the singleton doc on entry, purge for the test,
    restore afterwards so we never disturb an operator's real keys."""
    original = await db[KRAKEN_CREDENTIALS].find_one({"_id": "singleton"})
    await db[KRAKEN_CREDENTIALS].delete_one({"_id": "singleton"})
    # Force-clear env for a clean baseline every test — tests set
    # explicitly what they want to see.
    monkeypatch.delenv("KRAKEN_API_KEY", raising=False)
    monkeypatch.delenv("KRAKEN_API_SECRET", raising=False)
    yield
    await db[KRAKEN_CREDENTIALS].delete_one({"_id": "singleton"})
    if original is not None:
        await db[KRAKEN_CREDENTIALS].insert_one(original)


# ─── env fallback path ────────────────────────────────────────

@pytest.mark.asyncio
async def test_env_fallback_kicks_in_when_singleton_missing(monkeypatch):
    """No Mongo doc + both env vars set → status is `ok` with
    `source="env_fallback"`."""
    monkeypatch.setenv("KRAKEN_API_KEY", "env-pub-key-abc123")
    monkeypatch.setenv("KRAKEN_API_SECRET", "env-priv-key-secretvalue")

    status = await get_active_keys_status()
    assert status["state"] == "ok"
    assert status["source"] == "env_fallback"
    assert status["public_key"] == "env-pub-key-abc123"
    assert status["private_key"] == "env-priv-key-secretvalue"
    assert status["public_key_preview"] == "env-pu"
    assert "env" in status["detail"].lower()


@pytest.mark.asyncio
async def test_env_fallback_missing_both_returns_no_credentials(monkeypatch):
    """No Mongo doc + no env vars → state `no_credentials`, source
    None, detail explains both failures."""
    # Both env vars intentionally not set (fixture cleared them).
    status = await get_active_keys_status()
    assert status["state"] == "no_credentials"
    assert status["source"] is None
    assert "env fallback also empty" in status["detail"]


@pytest.mark.asyncio
async def test_env_fallback_missing_secret_only_returns_no_credentials(monkeypatch):
    """Only ONE env var set → must NOT partial-succeed. Fail-closed."""
    monkeypatch.setenv("KRAKEN_API_KEY", "only-pub-no-secret")
    status = await get_active_keys_status()
    assert status["state"] == "no_credentials"
    assert status["source"] is None


@pytest.mark.asyncio
async def test_env_fallback_when_singleton_has_empty_private_key(monkeypatch):
    """Singleton exists but `encrypted_private_key` is empty → the
    fallback path kicks in (previously returned `missing_field` with
    no way to recover)."""
    await db[KRAKEN_CREDENTIALS].insert_one({
        "_id": "singleton",
        "public_key": "old-pub",
        "encrypted_private_key": "",  # empty
    })
    monkeypatch.setenv("KRAKEN_API_KEY", "env-pub")
    monkeypatch.setenv("KRAKEN_API_SECRET", "env-priv")

    status = await get_active_keys_status()
    assert status["state"] == "ok"
    assert status["source"] == "env_fallback"


@pytest.mark.asyncio
async def test_env_fallback_when_decrypt_fails(monkeypatch):
    """Singleton exists but `decrypt()` raises (encryption-key drift
    on the deploy) → env fallback takes over gracefully."""
    await db[KRAKEN_CREDENTIALS].insert_one({
        "_id": "singleton",
        "public_key": "drifted-pub",
        "encrypted_private_key": "gibberish-that-wont-decrypt",
    })
    monkeypatch.setenv("KRAKEN_API_KEY", "env-pub-fallback")
    monkeypatch.setenv("KRAKEN_API_SECRET", "env-priv-fallback")

    status = await get_active_keys_status()
    assert status["state"] == "ok"
    assert status["source"] == "env_fallback"


# ─── happy Mongo path still wins ──────────────────────────────

@pytest.mark.asyncio
async def test_mongo_singleton_wins_over_env_when_healthy(monkeypatch):
    """When the singleton exists AND decrypts, MC prefers Mongo over
    env vars — single-source-of-truth doctrine when Mongo is healthy.
    Env vars are the fallback, not the override."""
    # Seed a decryptable singleton using the real credentials module.
    from shared.credentials import encrypt
    enc = encrypt("mongo-priv-truth")
    await db[KRAKEN_CREDENTIALS].insert_one({
        "_id": "singleton",
        "public_key": "mongo-pub-truth",
        "encrypted_private_key": enc,
        "public_key_preview": "mongo-",
    })
    # Set env vars to DIFFERENT values — they must NOT win.
    monkeypatch.setenv("KRAKEN_API_KEY", "env-pub-DECOY")
    monkeypatch.setenv("KRAKEN_API_SECRET", "env-priv-DECOY")

    status = await get_active_keys_status()
    assert status["state"] == "ok"
    assert status["source"] == "mongo_singleton"
    assert status["public_key"] == "mongo-pub-truth"
    assert status["private_key"] == "mongo-priv-truth"
