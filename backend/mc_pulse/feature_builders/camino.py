"""Canonical Camino feature builder.

Operator directive (2026-02, step 5 of the parity plan): one
canonical Camino feature builder for both the runner and the
pulse. The goal is NOT to widen the pulse's indicator set until
the parity endpoint proves what's actually missing — the goal is
to eliminate the possibility that runner and pulse differ because
of a subtle field-derivation drift.

Contract:
    Input: `bars` (chronological list of {ts, o, h, l, c, v},
           most recent LAST) + `lane` + optional
           `prior_daily_volumes` (list of prior sessions' daily
           volume totals from `shared_ohlcv_bars` at tf=1d).
    Output: the SAME snapshot dict shape that
            `NeutralAdversarialBrain._build_hypotheses` reads:
              price, price_change_pct, volume_change_pct, rsi,
              spread_bps, volatility, trend_score, liquidity_score,
              setup_score, spread_quality, market_regime, pattern,
              real_market_data, plus session_features enrichment
              (gap_pct, relative_volume, vwap_distance_pct, ...).

Cold-start fallback shape matches runner._build_snapshot's cold
branch exactly so the two paths behave identically when bars
are absent.

Why this file exists (design freeze §5 of the parity plan):
    The pulse Camino was calling the legacy core with an
    impoverished snapshot dict (only ema20, macd_hist, atr, ...).
    The core's defaults then pinned `hold_score` to 1.0 via
    `spread_bps=9999 * 0.002 = 19.98 → clamp → 1.0`, producing
    the "HOLD @ confidence 1.00" signature the operator flagged.
    Extracting one builder means the pulse and runner CANNOT
    silently diverge on the feature layer.
"""
from __future__ import annotations

import random
import time
from typing import Any, Optional

from shared.indicators import rsi as _rsi_series, session_features
from mc_pulse.feature_builders.coerce import optional_float


def build_camino_features(
    *,
    symbol: str,
    lane: str,
    bars: list[dict],
    prior_daily_volumes: Optional[list[float]] = None,
    market_regime: Optional[str] = None,
    spread_bps_override: Optional[float] = None,
    spread_quality: str = "live",
) -> tuple[dict, float]:
    """Build a Camino-shape snapshot from OHLCV bars.

    Returns `(snapshot, setup_score)`. Mirrors the runner's
    `_build_snapshot` exactly for the "hot" branch (>= 20 bars).
    Cold-start fallback returns synthetic drift with
    `real_market_data=False` so the runner and pulse produce
    parity-comparable "we had no bars" snapshots.

    Args:
        symbol: uppercase ticker.
        lane: "equity" | "crypto".
        bars: chronological list, most-recent LAST. Each bar
              carries at least `c` (close) and `v` (volume).
        prior_daily_volumes: optional pre-computed 20-day
              baseline (from `shared_ohlcv_bars` tf=1d). Feeds
              `session_features(prior_session_volumes=...)`.
        market_regime: tick-level regime tag; runner injects this
              from `_rank_universe`. Passed through to the returned
              snapshot's `market_regime` field so the core doesn't
              have to compute it.
        spread_bps_override: caller-provided spread (e.g. Webull
              live spread). Falls back to lane defaults if None.
        spread_quality: "live" | "stale" | "sentinel". Doctrine
              consumes this; the core substitutes spread_bps=25
              when quality is not "live".
    """
    # ── Hot branch: >= 20 usable bars → compute the full snapshot ──
    # 2026-07-11 doctrine step 5: never bare `float()` on
    # feeder-sourced data. `optional_float` returns None for
    # non-numeric / NaN / inf; we skip such rows silently so a
    # single bad bar can't crash the whole builder. If cleaning
    # leaves us with < 20 usable bars, fall through to the cold
    # branch — computing on a sparse window would be dishonest.
    closes: list[float] = []
    vols: list[float] = []
    if bars:
        for b in bars[-20:]:
            c = optional_float(b.get("c"))
            v = optional_float(b.get("v"))
            if c is None or v is None:
                continue
            closes.append(c)
            vols.append(v)

    if len(closes) >= 20:
        window_high = max(closes)
        window_low = min(closes) or 1.0
        volatility = (window_high - window_low) / window_low
        # `trend_score` uses first→last window return, then scaled
        # ×8 and clamped to [-1, 1]. Represents the 20-bar
        # directional bias — the "is this trending?" question.
        trend_return = (closes[-1] - closes[0]) / (closes[0] or 1.0)
        avg_vol = sum(vols) / max(len(vols), 1)
        recent_vol = sum(vols[-3:]) / 3
        vol_change_pct = (
            ((recent_vol - avg_vol) / avg_vol * 100.0) if avg_vol else 0.0
        )
        last_close = closes[-1]
        # 2026-07-14 fix (iter-29c): `price_change_pct` used to
        # inherit the window-return above — same value as trend_score
        # before the ×8 scaling. That made it a slow rolling bias
        # rather than the "just moved" signal GTO/Camino/Hellcat
        # were calibrated for (GTO's `PRICE_MAG=0.10` means "recent
        # bar moved 0.10%", NOT "window drifted 0.10%"). Fix: use
        # bar-over-bar percent change. Falls back to the window
        # return if only two bars are available so we still emit
        # a value.
        #
        # 2026-07-15 (iter-30 P2): three semantic axes now emitted
        # in parallel so different doctrines can consume EXACTLY
        # the time-scale they need without piggy-backing:
        #   - `bar_change_pct`     : last close vs previous close
        #                            (single-bar impulse — GTO,
        #                            Camino confirmation)
        #   - `session_change_pct` : from session open (comes from
        #                            `session_features`; None when
        #                            session grouping doesn't apply)
        #   - `window_change_pct`  : from window start to now
        #                            (20-bar drift — slow bias,
        #                            same period as `trend_score`
        #                            pre-scaling)
        # `price_change_pct` remains populated (= `bar_change_pct`)
        # for backward compat with any consumer we haven't migrated
        # yet + the input_manifest schema check.
        prev_close = closes[-2] if len(closes) >= 2 else closes[0]
        bar_change_pct = (
            ((last_close - prev_close) / prev_close * 100.0)
            if prev_close else 0.0
        )
        window_change_pct = trend_return * 100.0
        price_change_pct = bar_change_pct  # BC alias, same value
        # 2026-07-14 fix (iter-29c): RSI was hardcoded to 50.0 for
        # parity with the (now decommissioned) runner. Barracuda's
        # thresholds are 35/65 → constant 50.0 made Barracuda
        # mathematically incapable of firing on RSI. Compute the
        # real Wilder RSI from the same `closes` window. Requires
        # >14 bars for a real value; the 20-bar hot branch
        # guarantees that.
        rsi_series = _rsi_series(closes, period=14)
        latest_rsi = None
        for v in reversed(rsi_series):
            if v is not None:
                latest_rsi = round(v, 2)
                break
        # Should always be non-None here (20 closes → 6 real RSI
        # values) but keep the fallback for the edge case.
        rsi_val = latest_rsi if latest_rsi is not None else 50.0

        # Spread default: lane-specific, matches runner. Override
        # wins when caller supplies a live spread.
        override = optional_float(spread_bps_override)
        spread_bps = (
            override if override is not None
            else (8.0 if lane == "crypto" else 3.0)
        )

        snapshot: dict[str, Any] = {
            "symbol": symbol,
            "price": last_close,
            "price_change_pct": round(price_change_pct, 3),
            "bar_change_pct": round(bar_change_pct, 3),
            "window_change_pct": round(window_change_pct, 3),
            "volume_change_pct": round(vol_change_pct, 2),
            "rsi": rsi_val,
            "spread_bps": round(spread_bps, 2),
            "spread_quality": spread_quality,
            "volatility": round(min(1.0, max(0.0, volatility * 3)), 3),
            "trend_score": round(max(-1.0, min(1.0, trend_return * 8)), 3),
            "liquidity_score": 0.85,
            "market_regime": market_regime or "calm",
            "setup_score": 0.0,        # setup_score is computed elsewhere; caller may overwrite
            "pattern": "base_breakout",
            "real_market_data": True,
        }
        # Doctrine session enrichment — same call the runner makes.
        # `session_features` may return None for fields it can't
        # compute (e.g. `trend_score` when there aren't enough
        # same-session bars — the classic tf=1d symptom). Preserve
        # the hot-branch computation as a fallback so a single
        # daily bar doesn't erase the 20-bar window's directional
        # signal.
        hot_trend_score = snapshot.get("trend_score")
        snapshot.update(
            session_features(bars, prior_session_volumes=prior_daily_volumes),
        )
        # session_features may overwrite market_regime to None when
        # caller didn't pass one; restore our upstream regime tag
        # so downstream doctrine never sees a hole.
        if snapshot.get("market_regime") is None:
            snapshot["market_regime"] = market_regime or "calm"
        # Same rescue for trend_score. session_features' scoped
        # slope wins when it has one; hot-branch full-window slope
        # is used when the session slope is undefined. This
        # preserves the runner's current intraday behavior exactly
        # (session_features already returned a real number on
        # intraday windows) AND correctly reports direction on
        # daily-only windows where session_features returns None.
        if snapshot.get("trend_score") is None and hot_trend_score is not None:
            snapshot["trend_score"] = hot_trend_score
        return snapshot, 0.0

    # ── Cold branch: fewer than 20 bars → synthetic default ──
    # Identical shape to runner's cold branch so parity math can
    # still pair the two paths honestly (both marked
    # `real_market_data=False`).
    seed = hash(symbol) ^ (int(time.time()) // 300)
    rng = random.Random(seed)
    base = {
        "BTC/USD": 68000, "ETH/USD": 3400, "SOL/USD": 145, "ADA/USD": 0.45,
        "AAPL": 195, "MSFT": 420, "NVDA": 140, "TSLA": 250,
    }.get(symbol, 100.0)
    drift = rng.uniform(-2.5, 2.5)
    spread_bps = 8.0 if lane == "crypto" else 3.0
    return {
        "symbol": symbol,
        "price": round(base * (1 + drift / 100), 4),
        "price_change_pct": round(drift, 3),
        # 2026-07-15 (iter-30 P2): the three semantic axes carry the
        # same synthetic drift in the cold branch — a cold snapshot
        # has no real time-scale to distinguish them, so consistency
        # keeps downstream shape checks happy.
        "bar_change_pct": round(drift, 3),
        "session_change_pct": round(drift, 3),
        "window_change_pct": round(drift, 3),
        "volume_change_pct": round(rng.uniform(-30, 60), 2),
        "rsi": round(rng.uniform(28, 72), 1),
        "spread_bps": round(spread_bps + rng.uniform(0, 5), 2),
        "spread_quality": spread_quality,
        "volatility": round(rng.uniform(0.1, 0.7), 3),
        "trend_score": round(rng.uniform(-0.85, 0.85), 3),
        "liquidity_score": round(rng.uniform(0.5, 0.95), 3),
        "market_regime": market_regime or "calm",
        "setup_score": 0.0,
        "pattern": "cold_start_stub",
        "real_market_data": False,
    }, 0.0
