"""2026-02-20 — Equity-lane intent bridges (camino/barracuda/hellcat/gto).

Generated via `shared.intent_bridge_factory`. These tests pin:

  1. Symbol predicate is LANE-scoped, never pair/symbol-scoped.
     - "AAPL" passes; "BTC/USD" is rejected with crypto_only doctrine.
  2. `requires_final_authority` is sourced from `seats_with_execute("equity")`
     — never hardcoded to the emitting brain. (Operator's exact concern.)
  3. Research evidence flows through the SAME shared helper used by
     the crypto bridges — no per-brain copy of the doctrine.
  4. The `stack` field records the brain that authored the analysis,
     never the executing seat.
  5. HOLD action + non-equity symbol are still refused.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from shared.equity_intent_bridges import (
    build_camino_equity_intent,
    build_barracuda_equity_intent,
    build_hellcat_equity_intent,
    build_gto_equity_intent,
)
from shared.intent_bridge_factory import (
    BridgeConfig,
    looks_like_crypto,
    looks_like_equity,
    make_intent_bridge,
)


# ── Synthetic equity bull-run fixture (re-used from research tests) ──
def _bull_run(n: int = 80, start: float = 100.0) -> list[dict]:
    bars: list[dict] = []
    price = start
    for i in range(n):
        base = 0.2 + (i / n) * 0.6
        step = -base * 0.3 if i % 5 == 4 else base
        o = price
        c = price + step
        h = max(o, c) + 0.1
        l = min(o, c) - 0.1  # noqa: E741
        v = 1_000 if i < n - 3 else 5_000
        bars.append({"ts": i, "o": o, "h": h, "l": l, "c": c, "v": v})
        price = c
    return bars


@pytest.fixture(autouse=True)
def _stub_equity_seats(request):
    """Stub the equity seat holder. Each test that needs a specific
    holder can override via the `equity_holder` indirect param.

    Default holder = 'barracuda' (matches the current operator setup
    where barracuda owns crypto; equity has its own seat tree).
    """
    holder = getattr(request, "param", "camino")

    async def _hold(seat):
        return holder

    def _seats(lane):
        # Two equity seats today have may_execute=True; either is fine
        # for the factory's authority lookup. Returning one keeps the
        # test deterministic.
        return ["equity_executor"] if lane == "equity" else []

    with patch(
        "shared.intent_bridge_factory.get_seat_holder", new=_hold
    ), patch(
        "shared.intent_bridge_factory.seats_with_execute", new=_seats
    ):
        yield


# ── 1. Symbol predicates (lane-scoped, not pair-scoped) ──────────────
def test_looks_like_equity_accepts_real_tickers():
    for t in ("AAPL", "TSLA", "SPY", "QQQ", "BRK.B", "F"):
        assert looks_like_equity(t), t


def test_looks_like_equity_rejects_crypto_shapes():
    for t in ("BTC/USD", "ETH/USD", "BTC", "ETH", "SOLUSD", "BTCUSDT"):
        assert not looks_like_equity(t), t


def test_looks_like_crypto_still_accepts_pairs():
    for t in ("BTC/USD", "ETH/USD", "BTC", "ETH", "SOL", "SOLUSD"):
        assert looks_like_crypto(t), t


# ── 2. Factory respects lane-scoped final authority ──────────────────
@pytest.mark.asyncio
async def test_equity_bridge_authority_comes_from_equity_seat():
    # Camino emitting AAPL — final_authority should be whoever holds
    # the equity seat (here: stubbed to 'camino'), NOT hardcoded.
    async def _load(symbol, tf="1h", limit=120, source=None):
        return _bull_run(), "thinkorswim"

    with patch("shared.research.intent_evidence.load_recent_bars", new=_load):
        intent = await build_camino_equity_intent(
            symbol="AAPL",
            action="BUY",
            confidence=0.7,
            thesis="camino on AAPL",
        )
    assert intent["lane"] == "equity"
    assert intent["stack"] == "camino"
    assert intent["requires_final_authority"] == "camino"
    assert intent["requires_guard"] == "EquityRoadGuard"
    assert intent["intent_id"].startswith("alpha-equity-buy-")
    assert intent["ingest_method"] == "alpha_equity_bridge"
    assert intent["doctrine"]["equity_only"] is True


@pytest.mark.parametrize("_stub_equity_seats", ["barracuda"], indirect=True)
@pytest.mark.asyncio
async def test_seat_rotation_redirects_authority(_stub_equity_seats):
    """If operator rotates the equity seat to 'barracuda', a
    camino-authored intent stamps 'barracuda' as the final authority.
    Brain identity (stack) and execution authority are independent."""
    async def _load(symbol, tf="1h", limit=120, source=None):
        return _bull_run(), "thinkorswim"

    with patch("shared.research.intent_evidence.load_recent_bars", new=_load):
        intent = await build_camino_equity_intent(
            symbol="SPY",
            action="BUY",
            confidence=0.6,
            thesis="camino on SPY",
        )
    assert intent["stack"] == "camino"
    assert intent["requires_final_authority"] == "barracuda"


# ── 3. Research evidence flows through shared helper ─────────────────
@pytest.mark.asyncio
async def test_equity_bridge_stamps_research_signals():
    async def _load(symbol, tf="1h", limit=120, source=None):
        return _bull_run(), "thinkorswim"

    with patch("shared.research.intent_evidence.load_recent_bars", new=_load):
        intent = await build_gto_equity_intent(
            symbol="AAPL",
            action="BUY",
            confidence=0.7,
            thesis="gto on AAPL",
        )
    ev = intent["evidence"]
    assert ev["research_status"] == "ok"
    assert ev["research_source"] == "thinkorswim"
    assert ev["research_bars_used"] == 80
    assert ev["bridge"] == "redeye_equity_intent_bridge"
    sigs = ev["research_signals"]
    assert len(sigs) == 1
    assert sigs[0]["strategy_id"] == "large_cap_momentum_v1"
    assert sigs[0]["direction"] == "BUY"


# ── 4. Doctrine — non-equity symbol refused ──────────────────────────
@pytest.mark.asyncio
async def test_equity_bridge_refuses_crypto_symbol():
    with pytest.raises(Exception) as exc_info:
        await build_hellcat_equity_intent(
            symbol="BTC/USD",
            action="BUY",
            confidence=0.7,
            thesis="wrong lane",
        )
    assert "equity_only" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_equity_bridge_refuses_hold():
    with pytest.raises(Exception) as exc_info:
        await build_barracuda_equity_intent(
            symbol="AAPL",
            action="HOLD",  # type: ignore[arg-type]
            confidence=0.5,
            thesis="hold attempt",
        )
    assert "hold_not_promotable" in str(exc_info.value).lower()


# ── 5. All four equity bridges share identical doctrine surface ──────
@pytest.mark.asyncio
async def test_all_four_brains_produce_consistent_shape():
    async def _load(symbol, tf="1h", limit=120, source=None):
        return _bull_run(), "thinkorswim"

    builds = [
        ("camino",    build_camino_equity_intent,    "alpha"),
        ("barracuda", build_barracuda_equity_intent, "camaro"),
        ("hellcat",   build_hellcat_equity_intent,   "chevelle"),
        ("gto",       build_gto_equity_intent,       "redeye"),
    ]
    with patch("shared.research.intent_evidence.load_recent_bars", new=_load):
        for brain_id, builder, alias in builds:
            intent = await builder(
                symbol="AAPL",
                action="BUY",
                confidence=0.65,
                thesis=f"{brain_id} smoke",
            )
            assert intent["stack"] == brain_id
            assert intent["lane"] == "equity"
            assert intent["intent_id"].startswith(f"{alias}-equity-buy-")
            assert intent["ingest_method"] == f"{alias}_equity_bridge"
            assert intent["requires_guard"] == "EquityRoadGuard"
            # Lane-scoped authority — never hardcoded to the brain id.
            assert intent["requires_final_authority"] == "camino"  # from stub
            # Research evidence present & shape stable across brains.
            sigs = intent["evidence"]["research_signals"]
            assert sigs and sigs[0]["strategy_id"] == "large_cap_momentum_v1"


# ── 6. Unknown lane raises at factory time, not runtime ──────────────
def test_factory_rejects_unknown_lane():
    with pytest.raises(ValueError, match="unknown lane"):
        make_intent_bridge(BridgeConfig(
            brain_id="camino",
            lane="fx",          # type: ignore[arg-type]
            runtime_alias="alpha",
            roadguard_name="FxRoadGuard",
            route_prefix="/admin/camino/fx-bridge",
        ))
