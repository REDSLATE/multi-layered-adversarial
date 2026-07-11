"""Canonical Camino feature builder contract tests.

The whole point of extracting one builder shared by runner and
pulse is that a bar sequence maps to a SINGLE snapshot dict —
identical across runs, machines, and callers. These tests pin
the invariants that make parity comparisons semantically
trustworthy.
"""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone

import pytest

from mc_pulse.feature_builders.camino import build_camino_features
from mc_pulse.input_manifest import CAMINO_REQUIRED_FIELDS


def _bars(n: int, *, close_start: float = 100.0, close_step: float = 0.5,
          volume: float = 1000.0) -> list[dict]:
    """Manufacture a chronological bar window. Deterministic —
    identical seeds produce identical bars."""
    start = datetime(2026, 7, 11, 14, 0, 0, tzinfo=timezone.utc)
    bars = []
    for i in range(n):
        ts = (start + timedelta(minutes=i)).isoformat()
        close = round(close_start + i * close_step, 4)
        bars.append({
            "ts": ts,
            "o": round(close - 0.05, 4),
            "h": round(close + 0.10, 4),
            "l": round(close - 0.10, 4),
            "c": close,
            "v": volume,
        })
    return bars


# ─────────────────────── determinism ──────────────────────────────


def test_hot_branch_output_is_deterministic():
    """Same bars → identical snapshot (byte-equal after JSON dump).
    Non-determinism here would silently break every parity join."""
    bars_a = _bars(30)
    bars_b = _bars(30)
    snap_a, _ = build_camino_features(
        symbol="NVDA", lane="equity", bars=bars_a,
    )
    snap_b, _ = build_camino_features(
        symbol="NVDA", lane="equity", bars=bars_b,
    )
    assert snap_a == snap_b


def test_hot_branch_does_not_mutate_input_bars():
    """The builder must be a pure function of its inputs. If it
    mutates `bars`, a caller that reuses the list on the next
    tick would see drift and blame the strategy."""
    bars = _bars(30)
    frozen = copy.deepcopy(bars)
    build_camino_features(symbol="NVDA", lane="equity", bars=bars)
    assert bars == frozen


def test_hot_branch_does_not_mutate_prior_daily_volumes():
    bars = _bars(30)
    prior = [1_000_000.0, 2_000_000.0, 3_000_000.0]
    prior_frozen = list(prior)
    build_camino_features(
        symbol="NVDA", lane="equity", bars=bars,
        prior_daily_volumes=prior,
    )
    assert prior == prior_frozen


# ─────────────────────── required-field completeness ─────────────────


def test_hot_branch_produces_every_camino_required_field():
    """The whole reason we extracted this builder: the pulse
    Camino was starved of `spread_bps` / `trend_score` /
    `volatility` / ... and defaulted `hold_score` to 1.0. The hot
    branch MUST populate every required field."""
    bars = _bars(30)
    snap, _ = build_camino_features(
        symbol="NVDA", lane="equity", bars=bars,
    )
    missing = [f for f in CAMINO_REQUIRED_FIELDS if snap.get(f) is None]
    assert not missing, f"canonical builder missing required fields: {missing}"


def test_cold_branch_also_produces_required_fields():
    """The cold-start fallback is used by BOTH runner and pulse
    when bars < 20. Its schema must match the hot branch so
    parity math can pair (runner-cold, pulse-cold) as legitimately
    identical, not as "one path had features, the other didn't."
    """
    snap, _ = build_camino_features(
        symbol="NVDA", lane="equity", bars=[],
    )
    missing = [f for f in CAMINO_REQUIRED_FIELDS if snap.get(f) is None]
    assert not missing, f"cold-branch missing required fields: {missing}"


def test_cold_branch_marks_real_market_data_false():
    """Cold-start MUST be labeled explicitly — a runner-hot vs
    pulse-cold pair on the SAME symbol at the SAME close is a
    parity finding, not aggregate noise."""
    snap, _ = build_camino_features(symbol="NVDA", lane="equity", bars=[])
    assert snap["real_market_data"] is False


def test_hot_branch_marks_real_market_data_true():
    snap, _ = build_camino_features(
        symbol="NVDA", lane="equity", bars=_bars(30),
    )
    assert snap["real_market_data"] is True


# ─────────────────────── baseline / RVOL behavior ─────────────────────


def test_missing_daily_baseline_still_produces_snapshot():
    """When no prior-daily volumes are available (cold universe,
    weekend cache miss), the builder must still return a complete
    Camino snapshot — session_features cleanly returns None for
    RVOL and the rest of the snapshot fields carry on."""
    bars = _bars(30)
    snap, _ = build_camino_features(
        symbol="NVDA", lane="equity", bars=bars,
        prior_daily_volumes=None,
    )
    # `relative_volume` may be None, but the required scalars
    # must still be populated.
    for field in CAMINO_REQUIRED_FIELDS:
        assert snap.get(field) is not None


def test_baseline_present_populates_relative_volume():
    """With a real 20-day baseline, RVOL becomes a real number
    (not None) — parity math needs to see the same value on both
    paths when both pass the same baseline."""
    bars = _bars(30, volume=5000.0)
    prior = [1000.0] * 20        # 20 healthy prior sessions
    snap, _ = build_camino_features(
        symbol="NVDA", lane="equity", bars=bars,
        prior_daily_volumes=prior,
    )
    assert snap.get("relative_volume") is not None
    assert snap["relative_volume"] > 0.0


# ─────────────────────── lane defaults ───────────────────────


def test_equity_default_spread_bps_matches_runner():
    """Runner defaults equity to 3 bps when no override present.
    Diverging here would silently move HOLD/OBSERVE scores in the
    core."""
    snap, _ = build_camino_features(
        symbol="NVDA", lane="equity", bars=_bars(30),
    )
    assert snap["spread_bps"] == 3.0


def test_crypto_default_spread_bps_matches_runner():
    """Runner defaults crypto to 8 bps. Same rationale as above."""
    snap, _ = build_camino_features(
        symbol="BTC/USD", lane="crypto", bars=_bars(30),
    )
    assert snap["spread_bps"] == 8.0


def test_spread_bps_override_wins():
    """When a caller (Webull enrich) supplies a live spread, it
    replaces the lane default in both paths identically."""
    snap, _ = build_camino_features(
        symbol="NVDA", lane="equity", bars=_bars(30),
        spread_bps_override=1.5,
    )
    assert snap["spread_bps"] == 1.5


# ─────────────────────── market_regime pass-through ───────────


def test_market_regime_falls_through_from_caller():
    """The tick-level regime (from _rank_universe on the runner
    side, or its pulse-side equivalent) must reach the snapshot
    the same way in both paths."""
    snap, _ = build_camino_features(
        symbol="NVDA", lane="equity", bars=_bars(30),
        market_regime="vol_expansion",
    )
    assert snap["market_regime"] == "vol_expansion"


def test_market_regime_defaults_to_calm_when_absent():
    """Runner defaults to `market_regime="calm"` when nothing is
    injected. Pulse must match."""
    snap, _ = build_camino_features(
        symbol="NVDA", lane="equity", bars=_bars(30),
    )
    assert snap["market_regime"] == "calm"


# ─────────────────────── directional signal (↑ ↓ ↔) ─────────────────
#
# The parity work must not accidentally break Camino's ability to
# READ direction. These tests pin the canonical feature builder's
# response to the three canonical bar shapes:
#
#     ↑  monotonically rising closes  → trend_score > 0
#     ↓  monotonically falling closes → trend_score < 0
#     ↔  flat / sideways closes       → trend_score ≈ 0
#
# `trend_score` is the primary directional field the legacy
# `NeutralAdversarialBrain._build_hypotheses` reads to bias BUY
# vs SELL vs HOLD. If the sign flips or collapses here, the brain
# stops seeing the market.


def test_uptrend_produces_positive_trend_score():
    """Rising closes over 30 bars → strictly positive trend_score.
    Same magnitude range on runner and pulse (both use this builder)."""
    snap, _ = build_camino_features(
        symbol="NVDA", lane="equity",
        bars=_bars(30, close_start=100.0, close_step=0.5),
    )
    assert snap["trend_score"] > 0.0, (
        f"uptrend must produce positive trend_score, got {snap['trend_score']}"
    )
    assert snap["price_change_pct"] > 0.0


def test_downtrend_produces_negative_trend_score():
    """Falling closes over 30 bars → strictly negative trend_score."""
    snap, _ = build_camino_features(
        symbol="NVDA", lane="equity",
        bars=_bars(30, close_start=200.0, close_step=-0.5),
    )
    assert snap["trend_score"] < 0.0, (
        f"downtrend must produce negative trend_score, got {snap['trend_score']}"
    )
    assert snap["price_change_pct"] < 0.0


def test_sideways_produces_near_zero_trend_score():
    """Flat closes → trend_score at (or extremely close to) zero.
    A drift here would let Camino hallucinate direction from noise."""
    snap, _ = build_camino_features(
        symbol="NVDA", lane="equity",
        bars=_bars(30, close_start=100.0, close_step=0.0),
    )
    assert abs(snap["trend_score"]) < 0.05, (
        f"sideways must produce ~zero trend_score, got {snap['trend_score']}"
    )
    assert abs(snap["price_change_pct"]) < 0.05


def test_trend_score_sign_is_consistent_with_price_change_pct():
    """The two directional fields must agree in sign — they're
    read together by the core's hypothesis scorer. A disagreement
    would produce ambiguous BUY/SELL votes."""
    up, _ = build_camino_features(
        symbol="NVDA", lane="equity",
        bars=_bars(30, close_start=100.0, close_step=0.3),
    )
    down, _ = build_camino_features(
        symbol="NVDA", lane="equity",
        bars=_bars(30, close_start=200.0, close_step=-0.3),
    )
    assert (up["trend_score"] > 0) == (up["price_change_pct"] > 0)
    assert (down["trend_score"] < 0) == (down["price_change_pct"] < 0)


def test_trend_score_clamped_to_unit_interval():
    """`trend_score` is clamped to [-1, 1] by the builder. Even
    extreme parabolic moves must not exceed the interval — the
    core assumes it as a bounded feature."""
    parabolic_up, _ = build_camino_features(
        symbol="NVDA", lane="equity",
        bars=_bars(30, close_start=100.0, close_step=5.0),
    )
    parabolic_down, _ = build_camino_features(
        symbol="NVDA", lane="equity",
        bars=_bars(30, close_start=200.0, close_step=-3.0),
    )
    assert -1.0 <= parabolic_up["trend_score"] <= 1.0
    assert -1.0 <= parabolic_down["trend_score"] <= 1.0
