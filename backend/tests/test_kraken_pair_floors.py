"""Kraken per-pair notional floor — semantics contract (2026-02-17).

Locks in the doctrine set by the operator on 2026-02-17:

    Per-pair floor is expressed in USD notional (operator-native).
    policy="size_up"  → raise notional to the floor (default)
    policy="reject"   → terminate below the floor
    min_notional_usd=0 → EXPLICITLY UNGATED — never adjust
    Unknown pairs → env default (`KRAKEN_DEFAULT_MIN_NOTIONAL_USD`)
"""
from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, "/app")
sys.path.insert(0, "/app/backend")


def _fake_db_with_floors(rows: list[dict]):
    coll = MagicMock()
    class _Cur:
        def __init__(self, data): self._data = list(data)
        def sort(self, *a, **k): return self
        def __aiter__(self):
            self._i = 0
            return self
        async def __anext__(self):
            if self._i >= len(self._data):
                raise StopAsyncIteration
            r = self._data[self._i]; self._i += 1
            return r
    coll.find = MagicMock(return_value=_Cur(rows))
    db = MagicMock()
    db.__getitem__ = MagicMock(return_value=coll)
    return db


@pytest.mark.asyncio
async def test_apply_floor_size_up_raises_notional_to_floor():
    """Below floor + policy=size_up (default) → raise to floor."""
    from shared import kraken_pair_floors as m
    fake_db = _fake_db_with_floors([
        {"_id": "BTC/USD", "min_notional_usd": 5.0, "policy": "size_up"},
    ])
    with patch.object(m, "db", fake_db):
        m.invalidate_cache()
        r = await m.apply_floor("BTC/USD", 1.25)
    assert r.allowed is True
    assert r.notional_usd == 5.0
    assert r.adjusted is True
    assert r.original_notional == 1.25
    assert r.floor.is_default is False


@pytest.mark.asyncio
async def test_apply_floor_reject_terminates_with_clear_reason():
    """Below floor + policy=reject → terminate with reason."""
    from shared import kraken_pair_floors as m
    fake_db = _fake_db_with_floors([
        {"_id": "XRP/USD", "min_notional_usd": 3.0, "policy": "reject"},
    ])
    with patch.object(m, "db", fake_db):
        m.invalidate_cache()
        r = await m.apply_floor("XRP/USD", 0.5)
    assert r.allowed is False
    assert r.reject_reason is not None
    assert "notional_below_pair_floor" in r.reject_reason
    assert "$0.5" in r.reject_reason
    assert "$3.0" in r.reject_reason


@pytest.mark.asyncio
async def test_apply_floor_zero_is_ungated_never_adjust():
    """`min_notional_usd == 0` is the explicit ungated signal —
    honor operator intent, never raise."""
    from shared import kraken_pair_floors as m
    fake_db = _fake_db_with_floors([
        {"_id": "DOGE/USD", "min_notional_usd": 0, "policy": "size_up"},
    ])
    with patch.object(m, "db", fake_db):
        m.invalidate_cache()
        r = await m.apply_floor("DOGE/USD", 0.10)
    assert r.allowed is True
    assert r.notional_usd == 0.10
    assert r.adjusted is False


@pytest.mark.asyncio
async def test_apply_floor_above_floor_passes_through_unchanged():
    """Notional already above the floor → allowed, unchanged."""
    from shared import kraken_pair_floors as m
    fake_db = _fake_db_with_floors([
        {"_id": "BTC/USD", "min_notional_usd": 5.0, "policy": "size_up"},
    ])
    with patch.object(m, "db", fake_db):
        m.invalidate_cache()
        r = await m.apply_floor("BTC/USD", 12.50)
    assert r.allowed is True
    assert r.notional_usd == 12.50
    assert r.adjusted is False


@pytest.mark.asyncio
async def test_apply_floor_unknown_pair_falls_back_to_env_default():
    """No explicit config for the pair → use `KRAKEN_DEFAULT_MIN_NOTIONAL_USD`.
    Floor `.is_default` field surfaces this to the operator."""
    from shared import kraken_pair_floors as m
    fake_db = _fake_db_with_floors([])
    with patch.object(m, "db", fake_db), \
         patch.object(m, "DEFAULT_MIN_NOTIONAL_USD", 5.0):
        m.invalidate_cache()
        r = await m.apply_floor("SOMEPAIR/USD", 1.0)
    assert r.floor.is_default is True
    assert r.floor.min_notional_usd == 5.0
    assert r.allowed is True
    assert r.notional_usd == 5.0
    assert r.adjusted is True


@pytest.mark.asyncio
async def test_get_floor_returns_configured_row():
    from shared import kraken_pair_floors as m
    fake_db = _fake_db_with_floors([
        {"_id": "ETH/USD", "min_notional_usd": 8.0, "policy": "reject"},
    ])
    with patch.object(m, "db", fake_db):
        m.invalidate_cache()
        f = await m.get_floor("ETH/USD")
    assert f.min_notional_usd == 8.0
    assert f.policy == "reject"
    assert f.is_default is False


def test_allowed_policies_are_exactly_the_documented_pair():
    """If someone renames a policy without updating the module
    contract, this catches it."""
    from shared.kraken_pair_floors import ALLOWED_POLICIES
    assert ALLOWED_POLICIES == {"size_up", "reject"}


@pytest.mark.asyncio
async def test_invalidate_cache_forces_refetch():
    """After `invalidate_cache()`, the next call MUST re-read Mongo."""
    from shared import kraken_pair_floors as m
    fake_db_v1 = _fake_db_with_floors([
        {"_id": "BTC/USD", "min_notional_usd": 2.0, "policy": "size_up"},
    ])
    fake_db_v2 = _fake_db_with_floors([
        {"_id": "BTC/USD", "min_notional_usd": 10.0, "policy": "size_up"},
    ])
    with patch.object(m, "db", fake_db_v1):
        m.invalidate_cache()
        f1 = await m.get_floor("BTC/USD")
    assert f1.min_notional_usd == 2.0

    with patch.object(m, "db", fake_db_v2):
        # Without invalidate, cache would still return 2.0 (within TTL).
        m.invalidate_cache()
        f2 = await m.get_floor("BTC/USD")
    assert f2.min_notional_usd == 10.0
