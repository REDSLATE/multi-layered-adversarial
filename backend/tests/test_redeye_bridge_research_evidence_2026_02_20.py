"""2026-02-20 — GTO/redeye crypto bridge must stamp Research Layer
evidence onto every intent it builds.

Doctrine being pinned by these tests:
  * Evidence flows FROM Research Layer INTO intent["evidence"] only.
  * The bridge NEVER overwrites action / confidence / pipeline keys
    even when research disagrees with the brain.
  * If bars are missing for the symbol, the intent still emits and
    `evidence.research_status == "no_bars_on_file"`.
  * If `attach_research=False` is passed explicitly, no research
    fields appear on the intent — useful for unit tests of the
    bridge itself that don't want to mock the bar source.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from shared.redeye_crypto_intent_bridge import build_redeye_crypto_intent


# ── Synthetic helpers ────────────────────────────────────────────────
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
        return "gto"

    def _seats(lane):
        return ["crypto_executor"]

    with patch(
        "shared.redeye_crypto_intent_bridge.get_seat_holder", new=_hold
    ), patch(
        "shared.redeye_crypto_intent_bridge.seats_with_execute", new=_seats
    ):
        yield


# ── 1. Research evidence is attached on a normal build ───────────────
@pytest.mark.asyncio
async def test_build_stamps_research_signals_on_evidence():
    async def _load(symbol, tf="1h", limit=120, source=None):
        return _bear_breakdown(), "kraken_pro"

    with patch("shared.research.intent_evidence.load_recent_bars", new=_load):
        intent = await build_redeye_crypto_intent(
            symbol="BTC/USD",
            action="SELL",
            confidence=0.69,
            thesis="bearish breakdown",
        )

    assert intent["lane"] == "crypto"
    assert intent["stack"] == "gto"
    ev = intent["evidence"]
    assert ev["research_status"] == "ok"
    assert ev["research_source"] == "kraken_pro"
    assert ev["research_bars_used"] == 80
    assert isinstance(ev["research_signals"], list)
    assert len(ev["research_signals"]) == 1
    sig = ev["research_signals"][0]
    assert sig["strategy_id"] == "crypto_breakdown_v1"
    assert sig["direction"] == "SELL"
    assert "macd_bearish" in sig["reasons"]


# ── 2. Empty-bar case still emits a clean intent ─────────────────────
@pytest.mark.asyncio
async def test_build_no_bars_still_emits_with_status_marker():
    async def _empty(symbol, tf="1h", limit=120, source=None):
        return [], None

    with patch("shared.research.intent_evidence.load_recent_bars", new=_empty):
        intent = await build_redeye_crypto_intent(
            symbol="BTC/USD",
            action="SELL",
            confidence=0.69,
            thesis="bearish",
        )
    assert intent["evidence"]["research_status"] == "no_bars_on_file"
    assert intent["evidence"]["research_signals"] == []
    # Critically: the intent's decision fields are untouched.
    assert intent["action"] == "SELL"
    assert intent["confidence"] == 0.69
    assert intent["executed"] is False


# ── 3. Bar-source crash is contained ─────────────────────────────────
@pytest.mark.asyncio
async def test_build_research_error_is_swallowed():
    async def _boom(symbol, tf="1h", limit=120, source=None):
        raise RuntimeError("mongo timed out")

    with patch("shared.research.intent_evidence.load_recent_bars", new=_boom):
        intent = await build_redeye_crypto_intent(
            symbol="ETH/USD",
            action="SELL",
            confidence=0.71,
            thesis="weak rally",
        )
    assert intent["evidence"]["research_status"] == "error"
    assert "mongo timed out" in intent["evidence"]["research_error"]
    # Intent still flowed cleanly.
    assert intent["action"] == "SELL"
    assert intent["lane"] == "crypto"


# ── 4. attach_research=False bypasses the research layer entirely ────
@pytest.mark.asyncio
async def test_build_attach_research_false_skips_layer():
    # If `load_recent_bars` is touched at all this test will fail
    # because the symbol isn't actually in the DB during pytest.
    async def _explode(*a, **kw):
        raise AssertionError("research layer must not be called when attach_research=False")

    with patch("shared.research.intent_evidence.load_recent_bars", new=_explode):
        intent = await build_redeye_crypto_intent(
            symbol="BTC/USD",
            action="SELL",
            confidence=0.69,
            thesis="manual override",
            attach_research=False,
        )
    assert "research_status" not in intent["evidence"]
    assert "research_signals" not in intent["evidence"]


# ── 5. Doctrine — action and confidence never mutate ─────────────────
@pytest.mark.asyncio
async def test_research_never_overwrites_brain_decision_fields():
    # Even though the strategy fires SELL, an intent emitted as BUY
    # must remain BUY. Research is evidence, not authority.
    async def _load(symbol, tf="1h", limit=120, source=None):
        return _bear_breakdown(), "kraken_pro"

    with patch("shared.research.intent_evidence.load_recent_bars", new=_load):
        intent = await build_redeye_crypto_intent(
            symbol="BTC/USD",
            action="BUY",   # opposite of what the strategy will say
            confidence=0.55,
            thesis="contrarian long",
        )
    assert intent["action"] == "BUY"
    assert intent["confidence"] == 0.55
    # And research still saw the breakdown evidence — it's just on
    # the side, not in the driver's seat.
    sig = intent["evidence"]["research_signals"][0]
    assert sig["direction"] == "SELL"
