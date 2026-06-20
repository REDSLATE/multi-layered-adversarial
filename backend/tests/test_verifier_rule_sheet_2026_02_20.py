"""2026-02-20 — Verifier Rule Sheet + Brain Report Cards + Setup Memory.

Pins:
  1. Setup classifier is pure & stable across known patterns.
  2. MAE/MFE helper computes side-aware excursions correctly.
  3. Report card aggregator computes win-rate, profit-factor, avg
     mae/mfe correctly across a mixed-outcome lesson set.
  4. Setup memory adjuster:
     * KILL SWITCH OFF → multiplier == 1.0 always, no DB reads.
     * Insufficient samples → multiplier == 1.0.
     * Proven setup (high win rate) → boost capped at 1.20.
     * Broken setup (low win rate) → throttle floored at 0.50.
  5. `apply_setup_memory` only mutates evidence + confidence, never
     action / gate_state / pipeline keys.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.lessons.mae_mfe import compute_mae_mfe_bps
from shared.lessons.schemas import Lesson
from shared.lessons.setup_classifier import classify_setup
from shared.report_cards import _summarize  # internal but stable
from shared.setup_memory import (
    MULT_BOUND_MAX,
    MULT_BOUND_MIN,
    apply_setup_memory,
    compute_adjustment,
)


# ── 1. setup classifier ──────────────────────────────────────────────
def test_classify_agreement_returns_strategy_action():
    sigs = [{"strategy_id": "crypto_breakdown_v1", "direction": "SELL", "score": 0.7}]
    assert classify_setup("SELL", sigs) == "crypto_breakdown_v1:SELL"


def test_classify_contrarian_returns_contrarian_label():
    sigs = [{"strategy_id": "crypto_breakdown_v1", "direction": "SELL", "score": 0.7}]
    assert classify_setup("BUY", sigs) == "contrarian:BUY"


def test_classify_no_signals_returns_unscored():
    assert classify_setup("BUY", []) == "unscored:BUY"
    assert classify_setup("BUY", None) == "unscored:BUY"


def test_classify_hold_is_abstain():
    assert classify_setup("HOLD", [{"strategy_id": "x", "direction": "BUY", "score": 0.9}]) == "abstain"


def test_classify_only_hold_signal_falls_to_unscored():
    sigs = [{"strategy_id": "x", "direction": "HOLD", "score": 0.0}]
    assert classify_setup("BUY", sigs) == "unscored:BUY"


def test_classify_picks_highest_score_when_multiple_signals():
    sigs = [
        {"strategy_id": "weak", "direction": "BUY", "score": 0.2},
        {"strategy_id": "strong", "direction": "BUY", "score": 0.8},
    ]
    assert classify_setup("BUY", sigs) == "strong:BUY"


# ── 2. MAE/MFE helper ────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_mae_mfe_buy_long_side():
    # Fill at 100; bars range [98, 105]. BUY → MFE 500 bps, MAE 200 bps.
    bars = [
        {"ts": f"2026-01-01T0{i}:00:00+00:00", "o": 99 + i, "h": 100 + i, "l": 98 + i, "c": 99 + i, "v": 1}
        for i in range(7)
    ]
    with patch(
        "shared.lessons.mae_mfe.load_recent_bars",
        new=AsyncMock(return_value=(bars, "kraken_pro")),
    ):
        out = await compute_mae_mfe_bps(
            symbol="X", lane="crypto", side="BUY",
            fill_price=100.0, fill_ts="2026-01-01T00:00:00+00:00",
        )
    assert out["bars_used"] >= 2
    assert out["mfe_bps"] > 0
    assert out["mae_bps"] >= 0


@pytest.mark.asyncio
async def test_mae_mfe_sell_flips_sign():
    # Fill at 100; bars range [95, 102]. SELL → MFE 500 bps (price
    # went down), MAE 200 bps (briefly went up).
    bars = [
        {"ts": f"2026-01-01T0{i}:00:00+00:00", "o": 100 - i*0.5, "h": 101 - i*0.5, "l": 99 - i*0.5, "c": 100 - i*0.5, "v": 1}
        for i in range(6)
    ]
    bars[1]["h"] = 102  # brief upmove against the SELL
    with patch(
        "shared.lessons.mae_mfe.load_recent_bars",
        new=AsyncMock(return_value=(bars, "kraken_pro")),
    ):
        out = await compute_mae_mfe_bps(
            symbol="X", lane="crypto", side="SELL",
            fill_price=100.0, fill_ts="2026-01-01T00:00:00+00:00",
        )
    assert out["mfe_bps"] > 0
    assert out["mae_bps"] > 0


@pytest.mark.asyncio
async def test_mae_mfe_too_few_bars_returns_none():
    with patch(
        "shared.lessons.mae_mfe.load_recent_bars",
        new=AsyncMock(return_value=([], None)),
    ):
        out = await compute_mae_mfe_bps(
            symbol="X", lane="crypto", side="BUY",
            fill_price=100.0, fill_ts="2026-01-01T00:00:00+00:00",
        )
    assert out["mae_bps"] is None
    assert out["mfe_bps"] is None


# ── 3. Report card summarize ─────────────────────────────────────────
def _le(**kw):
    base = dict(
        intent_id="x", stack="hellcat", lane="crypto", symbol="BTC/USD",
        action="SELL", confidence=0.7,
    )
    base.update(kw)
    return Lesson(**base)


def test_summarize_basic_win_loss_split():
    lessons = [
        _le(outcome="win", pnl_bps=50.0, mae_bps=10.0, mfe_bps=60.0),
        _le(outcome="win", pnl_bps=80.0, mae_bps=12.0, mfe_bps=85.0),
        _le(outcome="loss", pnl_bps=-40.0, mae_bps=55.0, mfe_bps=8.0),
        _le(outcome="pending"),     # excluded from win-rate
    ]
    s = _summarize(lessons)
    assert s["intents_total"] == 4
    assert s["intents_resolved"] == 3
    assert s["pending"] == 1
    assert s["wins"] == 2
    assert s["losses"] == 1
    assert s["win_rate"] == round(2/3, 3)
    # PF = (50+80) / 40 = 3.25
    assert s["profit_factor"] == 3.25
    assert s["avg_pnl_bps"] == round((50 + 80 + (-40)) / 3, 2)


def test_summarize_empty_returns_safe_nones():
    s = _summarize([])
    assert s["intents_total"] == 0
    assert s["win_rate"] is None
    assert s["profit_factor"] is None
    assert s["avg_pnl_bps"] is None


# ── 4. Setup memory adjuster ─────────────────────────────────────────
@pytest.mark.asyncio
async def test_compute_adjustment_insufficient_samples_returns_neutral():
    # Force report-card to return a tiny resolved count.
    fake_card = {
        "overall": {"intents_resolved": 2, "win_rate": 0.5},
        "by_setup": {}, "by_regime": {}, "by_symbol_top": {},
    }
    with patch(
        "shared.setup_memory.build_report_card",
        new=AsyncMock(return_value=fake_card),
    ):
        block = await compute_adjustment(
            stack="hellcat", lane="crypto", action="SELL",
            research_signals=[
                {"strategy_id": "crypto_breakdown_v1", "direction": "SELL", "score": 0.7}
            ],
        )
    assert block["multiplier"] == 1.0
    assert block["reason"] == "insufficient_samples"


@pytest.mark.asyncio
async def test_compute_adjustment_proven_setup_boosts_capped():
    fake_card = {
        "overall": {"intents_resolved": 50, "win_rate": 0.75},
        "by_setup": {}, "by_regime": {}, "by_symbol_top": {},
    }
    with patch(
        "shared.setup_memory.build_report_card",
        new=AsyncMock(return_value=fake_card),
    ):
        block = await compute_adjustment(
            stack="hellcat", lane="crypto", action="SELL",
            research_signals=[
                {"strategy_id": "crypto_breakdown_v1", "direction": "SELL", "score": 0.7}
            ],
        )
    assert block["bucket"] == "proven"
    assert MULT_BOUND_MIN <= block["multiplier"] <= MULT_BOUND_MAX
    assert block["multiplier"] == 1.10


@pytest.mark.asyncio
async def test_compute_adjustment_broken_setup_floors_throttle():
    fake_card = {
        "overall": {"intents_resolved": 20, "win_rate": 0.10},
        "by_setup": {}, "by_regime": {}, "by_symbol_top": {},
    }
    with patch(
        "shared.setup_memory.build_report_card",
        new=AsyncMock(return_value=fake_card),
    ):
        block = await compute_adjustment(
            stack="gto", lane="equity", action="BUY",
            research_signals=[
                {"strategy_id": "large_cap_momentum_v1", "direction": "BUY", "score": 0.6}
            ],
        )
    assert block["bucket"] == "broken"
    assert block["multiplier"] == 0.50


# ── 5. apply_setup_memory mutation surface ───────────────────────────
@pytest.mark.asyncio
async def test_apply_setup_memory_kill_switch_off_no_op():
    intent = {
        "stack": "hellcat", "lane": "crypto", "action": "SELL",
        "confidence": 0.7,
        "evidence": {"research_signals": []},
        "executed": False,
        "gate_state": "pending",
    }
    pre_action = intent["action"]
    pre_conf = intent["confidence"]
    with patch(
        "shared.setup_memory.setup_memory_enabled",
        new=AsyncMock(return_value=False),
    ):
        await apply_setup_memory(intent)
    assert intent["action"] == pre_action
    assert intent["confidence"] == pre_conf
    assert intent["evidence"]["setup_memory"]["applied"] is False
    assert intent["evidence"]["setup_memory"]["reason"] == "kill_switch_off"


@pytest.mark.asyncio
async def test_apply_setup_memory_enabled_throttles_confidence():
    intent = {
        "stack": "gto", "lane": "equity", "action": "BUY",
        "confidence": 0.80,
        "evidence": {"research_signals": []},
        "executed": False,
        "gate_state": "pending",
    }
    fake_card = {
        "overall": {"intents_resolved": 30, "win_rate": 0.20},
        "by_setup": {}, "by_regime": {}, "by_symbol_top": {},
    }
    with patch(
        "shared.setup_memory.setup_memory_enabled",
        new=AsyncMock(return_value=True),
    ), patch(
        "shared.setup_memory.build_report_card",
        new=AsyncMock(return_value=fake_card),
    ):
        await apply_setup_memory(intent)
    # 0.20 win_rate falls into the "broken" bucket → 0.50× multiplier
    # so 0.80 → 0.40.
    assert intent["confidence"] == 0.40
    sm = intent["evidence"]["setup_memory"]
    assert sm["applied"] is True
    assert sm["bucket"] == "broken"
    assert sm["multiplier"] == 0.50
    assert sm["confidence_pre"] == 0.80
    assert sm["confidence_post"] == 0.40
    # Doctrine: action / gate fields untouched.
    assert intent["action"] == "BUY"
    assert intent["executed"] is False
    assert intent["gate_state"] == "pending"


@pytest.mark.asyncio
async def test_apply_setup_memory_compute_error_logged_and_intent_untouched():
    intent = {
        "stack": "hellcat", "lane": "crypto", "action": "SELL",
        "confidence": 0.69,
        "evidence": {"research_signals": []},
    }
    async def _boom(*a, **kw):
        raise RuntimeError("report_card unavailable")

    with patch(
        "shared.setup_memory.setup_memory_enabled",
        new=AsyncMock(return_value=True),
    ), patch(
        "shared.setup_memory.compute_adjustment",
        new=_boom,
    ):
        await apply_setup_memory(intent)
    assert intent["confidence"] == 0.69
    assert intent["evidence"]["setup_memory"]["applied"] is False
    assert intent["evidence"]["setup_memory"]["reason"] == "compute_error"
