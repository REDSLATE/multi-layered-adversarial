"""Weighted 2-of-3 confluence doctrine relaxation (2026-06).

Operator-approved: strict AND chains made Camino/GTO/Hellcat forfeit
almost every tick to Barracuda. Full confluence (3/3) keeps legacy
behavior; exactly one missing gate emits a dampened HALF-SIZE probe
(evidence.size_multiplier=0.5 — consumed by auto_router stage 2a-ii);
fewer than 2 gates still HOLDs.
"""
from __future__ import annotations

import pytest

from shared.brains._confluence import (
    PARTIAL_PENALTY,
    PARTIAL_SIZE_MULT,
    confluence_signal,
)
from shared.brains.camino.strategy import evaluate as camino_eval
from shared.brains.gto.strategy import evaluate as gto_eval
from shared.brains.hellcat.strategy import evaluate as hellcat_eval


# ───────────────────────── helper unit tests ─────────────────────────

def test_confluence_signal_modes():
    assert confluence_signal(0.8, (True, True, True)) == (0.8, "full", 3)
    sig, mode, passed = confluence_signal(0.8, (True, True, False))
    assert mode == "partial" and passed == 2
    assert sig == pytest.approx(0.8 * PARTIAL_PENALTY)
    assert confluence_signal(0.8, (True, False, False)) == (0.0, "none", 1)
    assert confluence_signal(0.8, (False, False, False)) == (0.0, "none", 0)


# ───────────────────────── camino (trend) ─────────────────────────

def _camino_ind(**over):
    base = {
        "ready": True,
        "last_close": 110.0,
        "rsi14": 60.0,
        "sma": {"20": 105.0, "50": 100.0},
        "ema": {"12": 108.0},
        "atr14": 2.0,
    }
    base.update(over)
    return base


def test_camino_full_confluence_full_size():
    d = camino_eval("AAPL", _camino_ind())
    assert d.action == "BUY"
    assert d.size_bias == 1.0
    assert "size_multiplier" not in d.evidence
    assert d.evidence["confluence"]["buy_mode"] == "full"
    assert d.evidence["confluence"]["buy_gates_passed"] == 3


def test_camino_partial_confluence_half_size_probe():
    # extended past EMA12 (+4%) → gate 3 fails, gates 1+2 hold
    d = camino_eval("AAPL", _camino_ind(ema={"12": 100.0}))
    assert d.action == "BUY"
    assert d.size_bias == PARTIAL_SIZE_MULT
    assert d.evidence["size_multiplier"] == PARTIAL_SIZE_MULT
    assert d.evidence["confluence"]["buy_mode"] == "partial"
    full = camino_eval("AAPL", _camino_ind())
    assert d.confidence < full.confidence
    assert "half-size probe" in d.rationale


def test_camino_one_gate_still_holds():
    # RSI 80 kills gate 2 AND its strength; ema gate 3 also fails
    d = camino_eval("AAPL", _camino_ind(rsi14=80.0, ema={"12": 100.0}))
    assert d.action == "HOLD"


# ───────────────────────── gto (momentum) ─────────────────────────

def _gto_ind(**over):
    base = {
        "ready": True,
        "last_close": 100.0,
        "rsi14": 70.0,
        "macd": {"hist": 0.01},
        "ema": {"12": 101.0, "26": 100.0},
        "sma": {"20": 99.0},
        "atr14": 1.5,
    }
    base.update(over)
    return base


def test_gto_full_confluence_full_size():
    d = gto_eval("MSFT", _gto_ind())
    assert d.action == "BUY"
    assert d.size_bias == 1.0
    assert "size_multiplier" not in d.evidence


def test_gto_partial_confluence_half_size_probe():
    # below SMA20 → gate 3 fails, macd>0 + ema up hold
    d = gto_eval("MSFT", _gto_ind(sma={"20": 101.0}))
    assert d.action == "BUY"
    assert d.size_bias == PARTIAL_SIZE_MULT
    assert d.evidence["size_multiplier"] == PARTIAL_SIZE_MULT
    assert d.evidence["confluence"]["buy_mode"] == "partial"


def test_gto_bearish_macd_zeroes_buy_strength():
    # macd<0 fails gate 1 AND contributes 0 to raw_buy — rsi alone
    # must carry the (penalized) signal or the brain holds.
    d = gto_eval("MSFT", _gto_ind(macd={"hist": -0.01}, rsi14=56.0))
    assert d.action == "HOLD"


# ───────────────────────── hellcat (breakout) ─────────────────────────

def _hellcat_ind(**over):
    base = {
        "ready": True,
        "last_close": 110.0,
        "rsi14": 70.0,
        "bbands": {"position": 0.95, "upper": 110.0, "lower": 90.0},
        "sma": {"20": 100.0},
        "atr14": 2.0,
    }
    base.update(over)
    return base


def test_hellcat_full_confluence_full_size():
    d = hellcat_eval("NVDA", _hellcat_ind())
    assert d.action == "BUY"
    assert d.size_bias == 1.0
    assert "size_multiplier" not in d.evidence


def test_hellcat_partial_confluence_half_size_probe():
    # upper band raised → not genuinely touching (gate 3 fails)
    d = hellcat_eval(
        "NVDA",
        _hellcat_ind(bbands={"position": 0.95, "upper": 115.0, "lower": 90.0}),
    )
    assert d.action == "BUY"
    assert d.size_bias == PARTIAL_SIZE_MULT
    assert d.evidence["size_multiplier"] == PARTIAL_SIZE_MULT
    assert d.evidence["confluence"]["buy_mode"] == "partial"


def test_hellcat_one_gate_still_holds():
    # bb_pos low + below band touch → only sma gate passes
    d = hellcat_eval(
        "NVDA",
        _hellcat_ind(bbands={"position": 0.5, "upper": 120.0, "lower": 90.0}),
    )
    assert d.action == "HOLD"
