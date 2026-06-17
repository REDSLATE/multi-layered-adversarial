"""Tests for the 2026-02-20 fractional-trading doctrine + seat patch.

Doctrine pin (operator):

    "Fractional does not make the signal better.
     Fractional makes the risk smaller."

This suite locks both layers:

  * Doctrine layer — large-cap + crypto baselines, FRACTIONAL_SUPPORTED
    label, BASELINE_ONLY_TOEHOLD detection & sizing clamp.
  * Seat layer    — `shared/broker/fractional_sizing.py` decision tree.
"""
from __future__ import annotations

import pytest

from shared.broker.fractional_sizing import (
    KRAKEN_FRACTIONAL_PRECISION,
    WEBULL_FRACTIONAL_MIN_USD,
    WEBULL_FRACTIONAL_PRECISION,
    FractionalSizingDecision,
    size_for_fractional,
)
from shared.crypto.doctrine.crypto_labels import label_crypto_snapshot
from shared.doctrine.large_cap_doctrine import _build_large_cap_labels
from shared.doctrine.large_cap_doctrine import (
    build_large_cap_doctrine_packet,
)


# ── Doctrine: large-cap baseline + fractional credit ──────────────


def test_large_cap_baseline_now_clears_c_quality():
    """Large-cap on a nothing-day must clear C_QUALITY (0.40) on
    baseline alone — pre-2026-02-20 baseline was 0.30 which dropped
    everything to REJECT."""
    snap = {
        "symbol": "AAPL",
        "lane": "equity",
        "spread_bps": 5.0,  # acceptable spread, no SPREAD_TOO_WIDE penalty
        "fractional_supported": False,  # bare baseline test
    }
    base = _build_large_cap_labels(snap)
    assert base.score == 0.40
    assert base.quality == "C_QUALITY"
    assert "LARGE_CAP_LIQUID" in base.labels
    assert "BASELINE_ONLY_TOEHOLD" in base.labels


def test_large_cap_fractional_label_adds_5_basis():
    """Fractional gives +0.05 — small (sizing unlock, NOT conviction)."""
    snap = {
        "symbol": "AAPL", "lane": "equity",
        "spread_bps": 5.0,
        "fractional_supported": True,
    }
    base = _build_large_cap_labels(snap)
    assert base.score == pytest.approx(0.45)
    assert "FRACTIONAL_SUPPORTED" in base.labels
    # Still BASELINE_ONLY because no real signal fired.
    assert "BASELINE_ONLY_TOEHOLD" in base.labels


def test_large_cap_b_quality_threshold_unchanged():
    """Critically: lifting the baseline must NOT promote nothing-burgers
    to B_QUALITY (0.60). Operator pin: brain conviction stays put;
    fractional only changes sizing."""
    snap = {
        "symbol": "AAPL", "lane": "equity",
        "spread_bps": 5.0, "fractional_supported": True,
    }
    base = _build_large_cap_labels(snap)
    assert base.quality == "C_QUALITY", "fractional must not promote to B"
    assert base.score < 0.60


def test_large_cap_real_signal_clears_b_quality():
    """A 1.5%+ gap and 1.5×+ rvol in a green tape on AAPL → B_QUALITY."""
    snap = {
        "symbol": "AAPL", "lane": "equity",
        "gap_pct": 1.5, "relative_volume": 1.6,
        "market_regime": "green_light", "spread_bps": 5.0,
        "fractional_supported": True,
    }
    base = _build_large_cap_labels(snap)
    # 0.40 + 0.05 + 0.15 + 0.15 + 0.10 = 0.85 (and clamped)
    assert base.score >= 0.60
    assert base.quality in {"A_QUALITY", "B_QUALITY"}
    assert "BASELINE_ONLY_TOEHOLD" not in base.labels


def test_large_cap_baseline_only_toehold_clamps_governor():
    """BASELINE_ONLY_TOEHOLD must clamp the governor risk_multiplier
    to ≤ 0.20 so 'nothing-burger' days trade at toehold size."""
    snap = {"symbol": "AAPL", "lane": "equity", "fractional_supported": True}
    pkt = build_large_cap_doctrine_packet(
        snap, seat_holders={"governor": "chevelle"},
    )
    governor = pkt["seats"]["governor"]
    assert governor["risk_multiplier"] <= 0.20, (
        f"baseline-only must clamp to toehold: got {governor['risk_multiplier']}"
    )


def test_large_cap_real_signal_does_not_clamp_to_toehold():
    """A real signal day must NOT be clamped — the brain earns full
    size when the doctrine actually fires labels."""
    snap = {
        "symbol": "AAPL", "lane": "equity",
        "gap_pct": 3.5, "relative_volume": 3.2,
        "market_regime": "green_light", "spread_bps": 5.0,
        "fractional_supported": True, "quality": "A_QUALITY",
    }
    pkt = build_large_cap_doctrine_packet(
        snap, seat_holders={"governor": "chevelle"},
    )
    governor = pkt["seats"]["governor"]
    assert governor["risk_multiplier"] > 0.20, (
        f"real-signal day should not be toehold-clamped: got {governor['risk_multiplier']}"
    )


# ── Doctrine: crypto baseline + fractional ────────────────────────


def test_crypto_baseline_now_above_zero():
    """Crypto baseline raised 0.00 → 0.20 + 0.05 fractional default.
    With completely empty snapshot, neutral labels (FUNDING_NEUTRAL,
    LIQUIDATION_BALANCED) fire and add their own credits — but the
    score must still be at least the baseline + fractional."""
    snap = {"symbol": "BTC", "lane": "crypto", "spread_bps": 5.0}
    base = label_crypto_snapshot(snap)
    # baseline (0.20) + fractional (0.05) + TIGHT_SPREAD (+0.15) +
    # FUNDING_NEUTRAL (+0.10) + LIQUIDATION_BALANCED (+0.10) = 0.60
    assert base.score >= 0.25  # at minimum: baseline + fractional
    assert "CRYPTO_LISTED" in base.labels
    assert "FRACTIONAL_SUPPORTED" in base.labels


def test_crypto_baseline_only_tags_toehold():
    """Snapshot with NO real signal (no volume, no trend, no OI exp)
    must tag BASELINE_ONLY_TOEHOLD regardless of neutral noise labels."""
    snap = {"symbol": "BTC", "lane": "crypto", "spread_bps": 5.0}
    base = label_crypto_snapshot(snap)
    # TIGHT_SPREAD is a quality-positive label → BASELINE_ONLY should
    # NOT fire when TIGHT_SPREAD is present. Use wide spread to get
    # a true baseline-only case:
    snap2 = {"symbol": "BTC", "lane": "crypto"}  # defaults to wide spread
    base2 = label_crypto_snapshot(snap2)
    assert "BASELINE_ONLY_TOEHOLD" in base2.labels


# ── Seat: fractional sizing decision tree ─────────────────────────


def test_whole_share_when_notional_covers_price():
    """$200 notional on $180 AAPL → whole-share (1 sh); fractional
    machinery skipped — fewer moving parts."""
    d = size_for_fractional(
        broker="webull", symbol="AAPL", notional_usd=200.0,
        last_price=180.0, lane="equity",
    )
    assert d.submission_mode == "WHOLE_SHARE"
    assert d.quantity == 1.0
    assert d.eligible is False  # whole-share path, not fractional


def test_webull_fractional_qty_mode(monkeypatch):
    """$10 budget on $180 AAPL in RTH → fractional QTY mode."""
    monkeypatch.setenv("RISEDUAL_BYPASS_MARKET_HOURS", "true")
    d = size_for_fractional(
        broker="webull", symbol="NVDA", notional_usd=10.0,
        last_price=180.0, lane="equity",
    )
    assert d.eligible is True
    assert d.submission_mode == "QTY"
    # 10 / 180 = 0.05555..., truncated to 5dp = 0.05555
    assert d.quantity == 0.05555
    assert "NVDA" not in d.reason  # operator-readable reason


def test_webull_fractional_amount_mode_when_price_unknown(monkeypatch):
    """No last_price → AMOUNT mode (Webull resolves qty server-side)."""
    monkeypatch.setenv("RISEDUAL_BYPASS_MARKET_HOURS", "true")
    d = size_for_fractional(
        broker="webull", symbol="NVDA", notional_usd=10.0,
        last_price=None, lane="equity",
    )
    assert d.eligible is True
    assert d.submission_mode == "AMOUNT"
    assert d.quantity is None


def test_webull_fractional_rejects_outside_rth(monkeypatch):
    """Webull fractional is RTH-only per their docs."""
    monkeypatch.delenv("RISEDUAL_BYPASS_MARKET_HOURS", raising=False)
    # is_equity_rth() reads the wall clock; we can't reliably make it
    # return False without monkeypatching it. Bypass-off + manual
    # patch of the helper:
    import shared.broker.fractional_sizing as fs
    monkeypatch.setattr(fs, "is_equity_rth", lambda: False)
    d = size_for_fractional(
        broker="webull", symbol="NVDA", notional_usd=10.0,
        last_price=180.0, lane="equity",
    )
    assert d.eligible is False
    assert d.submission_mode == "REJECT"
    assert "rth_only" in d.reason


def test_webull_fractional_rejects_below_min(monkeypatch):
    """Below Webull's $5 minimum → REJECT."""
    monkeypatch.setenv("RISEDUAL_BYPASS_MARKET_HOURS", "true")
    d = size_for_fractional(
        broker="webull", symbol="NVDA", notional_usd=3.0,
        last_price=180.0, lane="equity",
    )
    assert d.eligible is False
    assert d.submission_mode == "REJECT"
    assert "below_min" in d.reason
    assert f"${WEBULL_FRACTIONAL_MIN_USD:.2f}" in d.reason


def test_webull_fractional_rejects_blacklisted_symbol(monkeypatch):
    monkeypatch.setenv("RISEDUAL_BYPASS_MARKET_HOURS", "true")
    monkeypatch.setenv("WEBULL_FRACTIONAL_INELIGIBLE_SYMBOLS", "NVDA,GOOGL")
    d = size_for_fractional(
        broker="webull", symbol="NVDA", notional_usd=10.0,
        last_price=180.0, lane="equity",
    )
    assert d.eligible is False
    assert d.submission_mode == "REJECT"
    assert "blacklisted" in d.reason


def test_kraken_native_fractional():
    """Kraken every USD pair supports fractional natively."""
    d = size_for_fractional(
        broker="kraken", symbol="BTC", notional_usd=10.0,
        last_price=90_000.0, lane="crypto",
    )
    assert d.eligible is True
    assert d.submission_mode == "AMOUNT"
    # 10 / 90000 = 0.000111..., truncated to 8dp = 0.00011111
    assert d.quantity == pytest.approx(0.00011111, abs=1e-8)


def test_kraken_no_price_falls_back_to_amount():
    d = size_for_fractional(
        broker="kraken", symbol="BTC", notional_usd=10.0,
        last_price=None, lane="crypto",
    )
    assert d.eligible is True
    assert d.submission_mode == "AMOUNT"
    assert d.quantity is None


def test_unknown_broker_rejects():
    d = size_for_fractional(
        broker="alpaca", symbol="AAPL", notional_usd=10.0,
        last_price=180.0, lane="equity",
    )
    assert d.eligible is False
    assert d.submission_mode == "REJECT"


def test_truncation_not_rounding(monkeypatch):
    """7 / 13 = 0.5384615... — must truncate to 0.53846, not 0.53847.
    Rounding can push the order $0.01 over budget and trip the
    Webull min-order rail at $5.00 boundary."""
    monkeypatch.setenv("RISEDUAL_BYPASS_MARKET_HOURS", "true")
    d = size_for_fractional(
        broker="webull", symbol="AAPL", notional_usd=7.0,
        last_price=13.0, lane="equity",
    )
    # 7/13 = 0.53846153846..., truncated to 5dp = 0.53846
    assert d.quantity == 0.53846
    # Verify the implied notional is ≤ 7.00 (never exceeds budget):
    assert d.quantity * 13.0 <= 7.00
