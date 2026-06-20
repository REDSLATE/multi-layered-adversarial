"""2026-02-20 — Camino + Barracuda crypto bridges. Mirror of the
equity bridge tests (same factory, different lane).

Closes the matrix: every brain × every lane has an admin emit
surface. GTO + Hellcat keep their legacy crypto bridges (covered by
separate test files); these two pin the new factory-generated ones.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from shared.crypto_intent_bridges import (
    build_barracuda_crypto_intent,
    build_camino_crypto_intent,
)


def _bear_breakdown(n: int = 80, start: float = 100.0) -> list[dict]:
    bars: list[dict] = []
    price = start
    for i in range(n):
        base = 0.2 + (i / n) * 0.6
        step = base * 0.3 if i % 5 == 4 else -base
        o = price
        c = price + step
        h = max(o, c) + 0.1
        l = min(o, c) - 0.1  # noqa: E741
        v = 1_000 if i < n - 3 else 4_000
        bars.append({"ts": i, "o": o, "h": h, "l": l, "c": c, "v": v})
        price = c
    return bars


@pytest.fixture(autouse=True)
def _stub_crypto_seat():
    """Bridge requires a non-empty crypto seat. Stub for every test."""
    async def _hold(seat):
        return "barracuda"

    def _seats(lane):
        return ["crypto_executor"] if lane == "crypto" else []

    with patch(
        "shared.intent_bridge_factory.get_seat_holder", new=_hold
    ), patch(
        "shared.intent_bridge_factory.seats_with_execute", new=_seats
    ):
        yield


@pytest.mark.asyncio
async def test_camino_crypto_intent_builds_with_research():
    async def _load(symbol, tf="1h", limit=120, source=None):
        return _bear_breakdown(), "kraken_pro"

    with patch("shared.research.intent_evidence.load_recent_bars", new=_load):
        intent = await build_camino_crypto_intent(
            symbol="BTC/USD",
            action="SELL",
            confidence=0.7,
            thesis="camino on BTC",
        )
    assert intent["stack"] == "camino"
    assert intent["lane"] == "crypto"
    assert intent["intent_id"].startswith("alpha-crypto-sell-")
    assert intent["ingest_method"] == "alpha_crypto_bridge"
    assert intent["requires_guard"] == "CryptoRoadGuard"
    # Lane-scoped — final authority is the current crypto seat holder
    # (stubbed to barracuda), NOT the emitting brain.
    assert intent["requires_final_authority"] == "barracuda"
    ev = intent["evidence"]
    assert ev["research_status"] == "ok"
    sig = ev["research_signals"][0]
    assert sig["strategy_id"] == "crypto_breakdown_v1"
    assert sig["direction"] == "SELL"


@pytest.mark.asyncio
async def test_barracuda_crypto_intent_builds_with_research():
    async def _load(symbol, tf="1h", limit=120, source=None):
        return _bear_breakdown(), "kraken_pro"

    with patch("shared.research.intent_evidence.load_recent_bars", new=_load):
        intent = await build_barracuda_crypto_intent(
            symbol="ETH/USD",
            action="SELL",
            confidence=0.72,
            thesis="barracuda on ETH",
        )
    assert intent["stack"] == "barracuda"
    assert intent["lane"] == "crypto"
    assert intent["intent_id"].startswith("camaro-crypto-sell-")
    assert intent["ingest_method"] == "camaro_crypto_bridge"
    # Brain id == seat holder is a coincidence here; the assertion
    # is the lane-scoped lookup worked, not that they match.
    assert intent["requires_final_authority"] == "barracuda"


@pytest.mark.asyncio
async def test_crypto_factory_bridges_refuse_equity_symbol():
    with pytest.raises(Exception) as exc_info:
        await build_camino_crypto_intent(
            symbol="AAPL",
            action="BUY",
            confidence=0.6,
            thesis="wrong lane",
        )
    assert "crypto_only" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_crypto_factory_bridges_refuse_hold():
    with pytest.raises(Exception) as exc_info:
        await build_barracuda_crypto_intent(
            symbol="BTC/USD",
            action="HOLD",  # type: ignore[arg-type]
            confidence=0.5,
            thesis="hold attempt",
        )
    assert "hold_not_promotable" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_seat_rotation_redirects_crypto_authority():
    """Stub the crypto seat to camino — both bridges should stamp
    that as the final authority even though they're emitting from
    different brain identities."""
    async def _hold(seat):
        return "camino"

    def _seats(lane):
        return ["crypto_executor"] if lane == "crypto" else []

    async def _load(symbol, tf="1h", limit=120, source=None):
        return _bear_breakdown(), "kraken_pro"

    with patch(
        "shared.intent_bridge_factory.get_seat_holder", new=_hold,
    ), patch(
        "shared.intent_bridge_factory.seats_with_execute", new=_seats,
    ), patch(
        "shared.research.intent_evidence.load_recent_bars", new=_load,
    ):
        a = await build_camino_crypto_intent(
            symbol="BTC/USD", action="SELL", confidence=0.6, thesis="a",
        )
        b = await build_barracuda_crypto_intent(
            symbol="BTC/USD", action="SELL", confidence=0.6, thesis="b",
        )
    assert a["stack"] == "camino"
    assert b["stack"] == "barracuda"
    # Both intents addressed to the current seat holder, regardless
    # of which brain authored.
    assert a["requires_final_authority"] == "camino"
    assert b["requires_final_authority"] == "camino"
