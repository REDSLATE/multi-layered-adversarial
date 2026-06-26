"""Tests for /api/admin/brain-input-health.

Focuses on the per-symbol contract (which fields each brain requires
to evaluate) — the single source of truth must stay in sync with each
`shared/brains/<brain>/strategy.py::evaluate` `missing` check. If a
strategy adds a new dependency without updating this endpoint, the
operator's Brain Input Health tile will lie. These tests are the
guardrail.
"""
from __future__ import annotations

import sys

import pytest

sys.path.insert(0, "/app/backend")

from routes.admin_brain_input_health import (  # noqa: E402
    BRAIN_CHECKERS, _check_barracuda, _check_camino, _check_gto,
    _check_hellcat, MIN_RELIABLE_BARS, STALE_THRESHOLD_SEC,
)

from shared.brains.barracuda import strategy as barracuda_strategy  # noqa: E402
from shared.brains.gto import strategy as gto_strategy  # noqa: E402
from shared.brains.camino import strategy as camino_strategy  # noqa: E402
from shared.brains.hellcat import strategy as hellcat_strategy  # noqa: E402


def _complete_indicators():
    """An indicators dict satisfying ALL 4 brains' contracts."""
    return {
        "ready": True,
        "bars_seen": 300,
        "last_close": 100.0,
        "sma": {"20": 102.0, "50": 105.0, "200": 110.0},
        "ema": {"12": 100.5, "26": 102.0},
        "rsi14": 50.0,
        "macd": {"macd": 0.0, "signal": 0.0, "hist": 0.0},
        "bbands": {
            "mid": 102.0, "upper": 108.0, "lower": 96.0,
            "width_pct": 11.7, "position": 0.50,
        },
        "atr14": 1.5,
    }


@pytest.mark.parametrize("checker", [
    _check_barracuda, _check_gto, _check_camino, _check_hellcat,
])
def test_complete_indicators_satisfy_every_brain(checker):
    assert checker(_complete_indicators()) == []


# Cross-validate against each strategy's `missing` check — if the
# strategy returns a HOLD with skipped_reason='missing_indicators:...',
# the checker MUST agree the indicators are incomplete (else the tile
# tells the operator the brain CAN evaluate when in fact it can't).
@pytest.mark.parametrize("strategy,checker,drop_key,drop_value", [
    # Drop a top-level required field per brain
    (barracuda_strategy, _check_barracuda, "rsi14", None),
    (barracuda_strategy, _check_barracuda, "atr14", -1),  # invalid
    (gto_strategy, _check_gto, "rsi14", None),
    (camino_strategy, _check_camino, "rsi14", None),
    (hellcat_strategy, _check_hellcat, "rsi14", None),
])
def test_strategy_and_checker_agree_on_missing(
    strategy, checker, drop_key, drop_value,
):
    ind = _complete_indicators()
    if drop_value is None:
        del ind[drop_key]
    else:
        ind[drop_key] = drop_value
    # Strategy emits HOLD with missing_indicators reason
    decision = strategy.evaluate("AAPL", ind)
    assert decision.action == "HOLD"
    assert decision.skipped_reason is not None
    assert decision.skipped_reason.startswith("missing_indicators:")
    # Checker MUST flag the same field family
    missing = checker(ind)
    assert missing, (
        f"strategy {strategy.__name__} flagged {drop_key} as missing but "
        f"the input-health checker said it's complete — drift detected."
    )


def test_bb_position_missing_breaks_barracuda_and_hellcat():
    ind = _complete_indicators()
    ind["bbands"] = {"mid": 102.0, "upper": 108.0, "lower": 96.0}
    # position missing → both BB-using brains
    assert "bbands" in _check_barracuda(ind)
    assert "bbands" in _check_hellcat(ind)
    # but NOT GTO/Camino (they don't read bbands)
    assert "bbands" not in _check_gto(ind)
    assert "bbands" not in _check_camino(ind)


def test_macd_hist_missing_only_breaks_gto():
    ind = _complete_indicators()
    ind["macd"] = {"macd": 0.0, "signal": 0.0}  # no hist
    assert "macd_hist" in _check_gto(ind)
    assert _check_barracuda(ind) == []
    assert _check_camino(ind) == []
    assert _check_hellcat(ind) == []


def test_sma50_missing_breaks_barracuda_and_camino_only():
    ind = _complete_indicators()
    ind["sma"] = {"20": 102.0}  # drop 50
    assert "sma" in _check_barracuda(ind)
    assert "sma" in _check_camino(ind)
    # GTO/Hellcat only need sma20
    assert _check_gto(ind) == []
    assert _check_hellcat(ind) == []


def test_thresholds_are_operator_pinned():
    # Doctrine 2026-02-23: 10 min stale threshold = survives one missed
    # 60s refresh + one 5-min bar boundary. 60 bars = minimum where
    # SMA(50) and ATR(14) have full lookback + a sanity buffer.
    assert STALE_THRESHOLD_SEC == 10 * 60
    assert MIN_RELIABLE_BARS == 60


def test_brain_checkers_cover_exactly_four_brains():
    assert set(BRAIN_CHECKERS) == {"barracuda", "gto", "camino", "hellcat"}
    # Each entry is (doctrine_name, checker_callable)
    for brain_id, (doctrine, checker) in BRAIN_CHECKERS.items():
        assert isinstance(doctrine, str)
        assert callable(checker)
