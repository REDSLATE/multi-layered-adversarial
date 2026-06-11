"""Broker selection singleton — operator picks per-lane broker.

Covers the read/write/validation contract of `routes/broker_selection.py`.
"""
from __future__ import annotations

import pytest

from routes.broker_selection import (
    DEFAULT,
    VALID_CRYPTO,
    VALID_EQUITY,
    get_current_selection,
)


def test_defaults_preserved():
    # 2026-02-19: Public.com and Alpaca deprecated. Webull is the sole
    # equity broker. Kraken stays as the crypto default.
    assert DEFAULT == {"equity": "webull", "crypto": "kraken"}


def test_valid_equity_options_post_deprecation():
    # Equity is single-broker (Webull) — Public/Alpaca removed.
    assert VALID_EQUITY == {"webull"}


def test_valid_crypto_options_include_webull():
    assert "kraken" in VALID_CRYPTO
    assert "webull" in VALID_CRYPTO


@pytest.mark.asyncio
async def test_get_current_selection_returns_defaults_when_no_singleton(monkeypatch):
    class _FakeColl:
        async def find_one(self, _q):
            return None

    class _FakeDB:
        def __getitem__(self, _name):
            return _FakeColl()

    monkeypatch.setattr("routes.broker_selection.db", _FakeDB())
    sel = await get_current_selection()
    assert sel == {"equity": "webull", "crypto": "kraken"}


@pytest.mark.asyncio
async def test_get_current_selection_returns_persisted(monkeypatch):
    class _FakeColl:
        async def find_one(self, _q):
            return {"_id": "singleton", "equity": "webull", "crypto": "webull"}

    class _FakeDB:
        def __getitem__(self, _name):
            return _FakeColl()

    monkeypatch.setattr("routes.broker_selection.db", _FakeDB())
    sel = await get_current_selection()
    assert sel == {"equity": "webull", "crypto": "webull"}


@pytest.mark.asyncio
async def test_get_current_selection_fills_in_defaults_for_missing_lanes(monkeypatch):
    # Singleton has only `equity` — `crypto` must come from defaults.
    class _FakeColl:
        async def find_one(self, _q):
            return {"_id": "singleton", "equity": "webull"}

    class _FakeDB:
        def __getitem__(self, _name):
            return _FakeColl()

    monkeypatch.setattr("routes.broker_selection.db", _FakeDB())
    sel = await get_current_selection()
    assert sel["equity"] == "webull"
    assert sel["crypto"] == "kraken"  # filled from DEFAULT


@pytest.mark.asyncio
async def test_silent_coercion_legacy_public_to_webull(monkeypatch):
    """Production DB carries `{"equity": "public"}` from before the
    deprecation. Reads must NOT 500 — instead silently coerce to
    `"webull"` so the API stays alive across the migration.

    Regression for 2026-02-19 prod incident: removing `"public"` from
    `VALID_EQUITY` without coercion would crash the GET endpoint via
    Pydantic validation on the persisted row.
    """
    class _FakeColl:
        async def find_one(self, _q):
            return {"_id": "singleton", "equity": "public", "crypto": "kraken"}

    class _FakeDB:
        def __getitem__(self, _name):
            return _FakeColl()

    monkeypatch.setattr("routes.broker_selection.db", _FakeDB())
    sel = await get_current_selection()
    assert sel == {"equity": "webull", "crypto": "kraken"}


@pytest.mark.asyncio
async def test_silent_coercion_legacy_alpaca_to_webull(monkeypatch):
    """Same coercion contract for any `alpaca_paper` / `alpaca`
    legacy value that might still be on a stale row."""
    class _FakeColl:
        async def find_one(self, _q):
            return {"_id": "singleton", "equity": "alpaca_paper", "crypto": "kraken"}

    class _FakeDB:
        def __getitem__(self, _name):
            return _FakeColl()

    monkeypatch.setattr("routes.broker_selection.db", _FakeDB())
    sel = await get_current_selection()
    assert sel == {"equity": "webull", "crypto": "kraken"}
