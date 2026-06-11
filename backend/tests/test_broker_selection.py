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
    assert DEFAULT == {"equity": "public", "crypto": "kraken"}


def test_valid_equity_options_include_webull():
    assert "public" in VALID_EQUITY
    assert "webull" in VALID_EQUITY


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
    assert sel == {"equity": "public", "crypto": "kraken"}


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
