"""2026-02-20 — Hellcat/chevelle crypto bridge must stamp Research
Layer evidence on every intent it builds.

Mirrors the GTO/redeye bridge tests (same doctrine, same guard rails)
because the user directive was literally "Hellcat is next. Same way."
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from shared.chevelle_crypto_intent_bridge import build_hellcat_crypto_intent


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
def _stub_seat_holder():
    """Bridge requires a non-empty crypto seat. Stub it for every test."""
    async def _hold(seat):
        return "hellcat"

    def _seats(lane):
        return ["crypto_executor"]

    with patch(
        "shared.chevelle_crypto_intent_bridge.get_seat_holder", new=_hold
    ), patch(
        "shared.chevelle_crypto_intent_bridge.seats_with_execute", new=_seats
    ):
        yield


@pytest.mark.asyncio
async def test_hellcat_build_stamps_research_signals_on_evidence():
    async def _load(symbol, tf="1h", limit=120, source=None):
        return _bear_breakdown(), "kraken_pro"

    with patch("shared.research.intent_evidence.load_recent_bars", new=_load):
        intent = await build_hellcat_crypto_intent(
            symbol="ETH/USD",
            action="SELL",
            confidence=0.71,
            thesis="ETH bearish breakdown",
        )

    assert intent["stack"] == "hellcat"
    assert intent["lane"] == "crypto"
    assert intent["symbol"] == "ETH/USD"
    assert intent["ingest_method"] == "chevelle_crypto_bridge"
    ev = intent["evidence"]
    assert ev["research_status"] == "ok"
    assert ev["research_source"] == "kraken_pro"
    assert ev["research_bars_used"] == 80
    assert ev["bridge"] == "chevelle_crypto_intent_bridge"
    sigs = ev["research_signals"]
    assert len(sigs) == 1
    assert sigs[0]["strategy_id"] == "crypto_breakdown_v1"
    assert sigs[0]["direction"] == "SELL"
    assert "macd_bearish" in sigs[0]["reasons"]


@pytest.mark.asyncio
async def test_hellcat_no_bars_still_emits_clean():
    async def _empty(symbol, tf="1h", limit=120, source=None):
        return [], None

    with patch("shared.research.intent_evidence.load_recent_bars", new=_empty):
        intent = await build_hellcat_crypto_intent(
            symbol="ETH/USD",
            action="SELL",
            confidence=0.71,
            thesis="cold start",
        )
    assert intent["evidence"]["research_status"] == "no_bars_on_file"
    assert intent["evidence"]["research_signals"] == []
    assert intent["action"] == "SELL"
    assert intent["confidence"] == 0.71
    assert intent["executed"] is False


@pytest.mark.asyncio
async def test_hellcat_research_error_contained():
    async def _boom(symbol, tf="1h", limit=120, source=None):
        raise RuntimeError("mongo unreachable")

    with patch("shared.research.intent_evidence.load_recent_bars", new=_boom):
        intent = await build_hellcat_crypto_intent(
            symbol="ETH/USD",
            action="SELL",
            confidence=0.71,
            thesis="error path",
        )
    assert intent["evidence"]["research_status"] == "error"
    assert "mongo unreachable" in intent["evidence"]["research_error"]
    assert intent["action"] == "SELL"


@pytest.mark.asyncio
async def test_hellcat_research_does_not_overwrite_brain_decision():
    """Strategy says SELL, brain says BUY → brain wins; research is
    side-channel evidence only."""
    async def _load(symbol, tf="1h", limit=120, source=None):
        return _bear_breakdown(), "kraken_pro"

    with patch("shared.research.intent_evidence.load_recent_bars", new=_load):
        intent = await build_hellcat_crypto_intent(
            symbol="ETH/USD",
            action="BUY",       # opposite of strategy
            confidence=0.55,
            thesis="hellcat contrarian long",
        )
    assert intent["action"] == "BUY"
    assert intent["confidence"] == 0.55
    sig = intent["evidence"]["research_signals"][0]
    assert sig["direction"] == "SELL"


@pytest.mark.asyncio
async def test_hellcat_attach_research_false_bypasses_layer():
    async def _explode(*a, **kw):
        raise AssertionError("research layer must not be called when attach_research=False")

    with patch("shared.research.intent_evidence.load_recent_bars", new=_explode):
        intent = await build_hellcat_crypto_intent(
            symbol="ETH/USD",
            action="SELL",
            confidence=0.71,
            thesis="manual override",
            attach_research=False,
        )
    assert "research_status" not in intent["evidence"]
    assert "research_signals" not in intent["evidence"]


@pytest.mark.asyncio
async def test_hellcat_refuses_hold_action():
    with pytest.raises(Exception) as exc_info:
        await build_hellcat_crypto_intent(
            symbol="ETH/USD",
            action="HOLD",          # type: ignore[arg-type]
            confidence=0.5,
            thesis="hold attempt",
        )
    assert "hold_not_promotable" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_hellcat_refuses_non_crypto_symbol():
    with pytest.raises(Exception) as exc_info:
        await build_hellcat_crypto_intent(
            symbol="AAPL",
            action="SELL",
            confidence=0.7,
            thesis="not crypto",
        )
    assert "crypto_only" in str(exc_info.value).lower()
