"""Small-cap base-formation → consolidation → explosive-breakout pattern detector.

Doctrine (2026-05-27, operator-confirmed):
    MC stamps evidence. Brains judge evidence. Seat holder acts.

    This module computes THREE deterministic boolean signals + a
    descriptive composite score over a window of OHLCV bars:

      1. ma200_uptrend_active  — MA200 slope > 0 over trailing N bars
      2. consolidation_zone    — price range / MA200 ≤ threshold over
                                 a min-duration window, MAs converged,
                                 sustained volume
      3. explosive_breakout    — close > consolidation ceiling × 1.02,
                                 volume ≥ 1.8× 20-bar average, within
                                 the last K bars

    The composite `setup_score ∈ [0, 1]` is a weighted summary. It is
    PURELY DESCRIPTIVE — never a gate, never a hard block, never
    modifies authority. Brains read it and decide what to weight.

Pure-function module. No DB, no FastAPI, no env reads beyond the
module-level config defaults (resolved at import time so tests can
monkeypatch + `reload_env`). The detector takes a window of bars
(oldest → newest, each {o,h,l,c,v,ts}) and returns a
`PatternSignals` dataclass. Storage + endpoint wiring live elsewhere.
"""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from typing import Optional


# ──────────────────────── env-tunable thresholds ────────────────────────


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, str(default)))
    except (TypeError, ValueError):
        return default


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, str(default)))
    except (TypeError, ValueError):
        return default


# Module-level config (resolved at import, reloadable via reload_env).
# These match operator-approved defaults — tune via env, not by editing.
class Config:
    """Mutable container so `reload_env` can refresh values without
    needing to rebind every consumer."""
    # MA200 uptrend: slope must be > 0 over the trailing N bars.
    ma200_uptrend_bars: int = _env_int("PATTERN_MA200_UPTREND_BARS", 30)
    # Consolidation zone:
    consolidation_range_max_pct: float = _env_float(
        "PATTERN_CONSOLIDATION_RANGE_MAX_PCT", 0.12,   # ≤ 12% of MA200
    )
    consolidation_min_bars: int = _env_int(
        "PATTERN_CONSOLIDATION_MIN_BARS", 20,
    )
    ma_convergence_max_pct: float = _env_float(
        "PATTERN_MA_CONVERGENCE_MAX_PCT", 0.03,        # MA 5/10/20/50 ≤ 3% spread
    )
    # Explosive breakout:
    breakout_ceiling_mult: float = _env_float(
        "PATTERN_BREAKOUT_CEILING_MULT", 1.02,         # close ≥ ceiling × 1.02
    )
    breakout_volume_mult: float = _env_float(
        "PATTERN_BREAKOUT_VOLUME_MULT", 1.8,           # vol ≥ 1.8× 20-bar avg
    )
    breakout_window_bars: int = _env_int(
        "PATTERN_BREAKOUT_WINDOW_BARS", 5,
    )
    # Small-cap qualifier:
    # If the brain provides a `float_shares` (in millions) on the
    # technical request, MC stamps `small_cap_qualified` when float
    # is ≤ this threshold. None / missing → flag stays None (unknown).
    small_cap_float_max_millions: float = _env_float(
        "PATTERN_SMALL_CAP_FLOAT_MAX_MILLIONS", 20.0,
    )


def reload_env() -> None:
    """Re-read env vars. Lets tests + the operator tighten thresholds
    mid-session without a redeploy."""
    Config.ma200_uptrend_bars = _env_int("PATTERN_MA200_UPTREND_BARS", 30)
    Config.consolidation_range_max_pct = _env_float(
        "PATTERN_CONSOLIDATION_RANGE_MAX_PCT", 0.12,
    )
    Config.consolidation_min_bars = _env_int(
        "PATTERN_CONSOLIDATION_MIN_BARS", 20,
    )
    Config.ma_convergence_max_pct = _env_float(
        "PATTERN_MA_CONVERGENCE_MAX_PCT", 0.03,
    )
    Config.breakout_ceiling_mult = _env_float(
        "PATTERN_BREAKOUT_CEILING_MULT", 1.02,
    )
    Config.breakout_volume_mult = _env_float(
        "PATTERN_BREAKOUT_VOLUME_MULT", 1.8,
    )
    Config.breakout_window_bars = _env_int(
        "PATTERN_BREAKOUT_WINDOW_BARS", 5,
    )
    Config.small_cap_float_max_millions = _env_float(
        "PATTERN_SMALL_CAP_FLOAT_MAX_MILLIONS", 20.0,
    )


# ──────────────────────── helpers ────────────────────────


def _sma(values: list[float], period: int) -> Optional[float]:
    """Single-value SMA over the most recent `period` entries.
    Returns None if not enough data."""
    if len(values) < period:
        return None
    window = values[-period:]
    return sum(window) / period


def _series_sma(values: list[float], period: int) -> list[Optional[float]]:
    """Aligned SMA series — None for warm-up slots, like indicators.sma."""
    out: list[Optional[float]] = []
    s = 0.0
    for i, v in enumerate(values):
        s += v
        if i >= period:
            s -= values[i - period]
        out.append(s / period if i >= period - 1 else None)
    return out


def _ma200_slope(sma200_series: list[Optional[float]], bars: int) -> Optional[float]:
    """Slope of MA200 over the trailing N bars. Per-bar change in price.
    Returns None if MA200 warm-up isn't complete across the whole window.
    Positive value ⇒ uptrend.
    """
    if bars < 2 or len(sma200_series) < bars:
        return None
    tail = sma200_series[-bars:]
    if any(v is None for v in tail):
        return None
    return (tail[-1] - tail[0]) / (bars - 1)


# ──────────────────────── signal dataclasses ────────────────────────


@dataclass
class MA200Uptrend:
    active: bool
    slope_per_bar: Optional[float]
    bars_evaluated: int
    ma200_now: Optional[float]
    ma200_then: Optional[float]


@dataclass
class ConsolidationZone:
    active: bool
    floor: Optional[float] = None
    ceiling: Optional[float] = None
    duration_bars: int = 0
    range_pct_of_ma200: Optional[float] = None
    ma_convergence_score: Optional[float] = None
    volume_accumulation_score: Optional[float] = None
    reason: str = ""


@dataclass
class ExplosiveBreakout:
    active: bool
    breakout_pct: Optional[float] = None
    volume_surge_multiple: Optional[float] = None
    bars_since_breakout: Optional[int] = None
    ceiling_referenced: Optional[float] = None
    reason: str = ""


@dataclass
class PatternSignals:
    """Top-level descriptive evidence packet. Stamped on the technical
    feed and persisted to `shared_pattern_snapshots` for replay."""
    ready: bool
    bars_seen: int
    tf: Optional[str]
    symbol: Optional[str]
    last_close: Optional[float]
    last_bar_ts: Optional[str]
    ma200_uptrend: dict
    consolidation: dict
    breakout: dict
    setup_score: float           # ∈ [0, 1] — composite descriptive
    small_cap_qualified: Optional[bool]   # None when float_shares unknown
    config_snapshot: dict
    doctrine_note: str = field(default=(
        "Descriptive evidence only. Never a gate, never authority. "
        "Brains read; seat holder acts."
    ))


# ──────────────────────── core detector ────────────────────────


def detect_pattern(
    bars: list[dict],
    *,
    symbol: Optional[str] = None,
    tf: Optional[str] = None,
    float_shares_millions: Optional[float] = None,
) -> PatternSignals:
    """Compute the 3 signals + composite score from a window of bars.

    `bars` must be sorted oldest → newest, each carrying `o,h,l,c,v`
    (floats coercible) and `ts` (ISO string or None). Bars are taken
    as-is — no resampling, no gap-filling.

    Bar count requirements:
      * MA200 uptrend needs ≥ 200 + `ma200_uptrend_bars` bars to be
        evaluable. Falls back to a typed `ready=True, active=False`
        verdict with `bars_evaluated=0` when insufficient.
      * Consolidation + breakout need at least `consolidation_min_bars`
        + `breakout_window_bars` of recent history. Less than that:
        the relevant signal returns `active=False` with a typed reason.

    Returns a `PatternSignals` whose `ready` flag is True whenever
    we processed bars (even if all three signals are inactive). The
    caller can serialize via `asdict` for storage / API response.
    """
    n = len(bars)
    if n == 0:
        return PatternSignals(
            ready=False, bars_seen=0, tf=tf, symbol=symbol,
            last_close=None, last_bar_ts=None,
            ma200_uptrend=asdict(MA200Uptrend(False, None, 0, None, None)),
            consolidation=asdict(ConsolidationZone(False, reason="no_bars")),
            breakout=asdict(ExplosiveBreakout(False, reason="no_bars")),
            setup_score=0.0,
            small_cap_qualified=_small_cap_flag(float_shares_millions),
            config_snapshot=_config_snapshot(),
        )

    closes = [float(b["c"]) for b in bars]
    highs = [float(b["h"]) for b in bars]
    lows = [float(b["l"]) for b in bars]
    vols = [float(b["v"]) for b in bars]
    last_close = closes[-1]
    last_bar_ts = bars[-1].get("ts")

    # ─── Signal 1: MA200 uptrend ───
    sma200_series = _series_sma(closes, 200)
    slope = _ma200_slope(sma200_series, Config.ma200_uptrend_bars)
    ma200_now = sma200_series[-1] if sma200_series else None
    ma200_then = None
    if (
        slope is not None
        and len(sma200_series) >= Config.ma200_uptrend_bars
    ):
        ma200_then = sma200_series[-Config.ma200_uptrend_bars]
    ma200_uptrend_active = slope is not None and slope > 0.0
    ma200_signal = MA200Uptrend(
        active=ma200_uptrend_active,
        slope_per_bar=slope,
        bars_evaluated=(
            Config.ma200_uptrend_bars if slope is not None else 0
        ),
        ma200_now=ma200_now,
        ma200_then=ma200_then,
    )

    # ─── Signal 2: Consolidation zone ───
    consol = _detect_consolidation(
        highs, lows, closes, vols, sma200_series,
    )

    # ─── Signal 3: Explosive breakout ───
    breakout = _detect_breakout(
        closes, vols, consol,
    )

    # ─── Composite score ───
    score = _composite_score(ma200_signal, consol, breakout)

    return PatternSignals(
        ready=True,
        bars_seen=n,
        tf=tf,
        symbol=symbol,
        last_close=last_close,
        last_bar_ts=last_bar_ts,
        ma200_uptrend=asdict(ma200_signal),
        consolidation=asdict(consol),
        breakout=asdict(breakout),
        setup_score=score,
        small_cap_qualified=_small_cap_flag(float_shares_millions),
        config_snapshot=_config_snapshot(),
    )


# ──────────────────────── signal-specific helpers ────────────────────────


def _detect_consolidation(
    highs: list[float], lows: list[float], closes: list[float],
    vols: list[float], sma200_series: list[Optional[float]],
) -> ConsolidationZone:
    """Look for a sideways window of at least `consolidation_min_bars`
    where (ceiling - floor) / MA200 ≤ `consolidation_range_max_pct`,
    and the short MAs are converged.

    We scan backward from the most-recent bar. The window MUST END
    within the breakout_window_bars (so the breakout can reference
    a freshly-finished consolidation, not one that ended weeks ago).
    """
    min_bars = Config.consolidation_min_bars
    breakout_window = Config.breakout_window_bars
    n = len(closes)

    if n < min_bars + 1:
        return ConsolidationZone(False, reason="insufficient_bars")

    # The consolidation window ends at `end_idx` and contains
    # `[end_idx - duration + 1 .. end_idx]`. We try end_idx slipping
    # back within `breakout_window` bars from the latest bar.
    best: Optional[ConsolidationZone] = None
    last_idx = n - 1
    end_min = max(min_bars - 1, last_idx - breakout_window)
    for end_idx in range(last_idx, end_min - 1, -1):
        start_idx = end_idx - min_bars + 1
        if start_idx < 0:
            break

        ma200_here = sma200_series[end_idx] if sma200_series else None
        if ma200_here is None or ma200_here <= 0:
            # MA200 warm-up incomplete here.
            continue

        # Extend the window backward as long as the range stays tight.
        cur_start = start_idx
        while cur_start - 1 >= 0:
            cand_floor = min(lows[cur_start - 1: end_idx + 1])
            cand_ceiling = max(highs[cur_start - 1: end_idx + 1])
            cand_range_pct = (cand_ceiling - cand_floor) / ma200_here
            if cand_range_pct <= Config.consolidation_range_max_pct:
                cur_start -= 1
            else:
                break

        floor_v = min(lows[cur_start: end_idx + 1])
        ceiling_v = max(highs[cur_start: end_idx + 1])
        range_pct = (ceiling_v - floor_v) / ma200_here

        if range_pct > Config.consolidation_range_max_pct:
            continue  # too wide — try a different end_idx

        duration = end_idx - cur_start + 1
        if duration < min_bars:
            continue

        # MA convergence (5/10/20/50) at end_idx — all within
        # `ma_convergence_max_pct` of their mean.
        ma_score = _ma_convergence_score(closes, end_idx)

        # Volume accumulation — mean volume over window vs the
        # `min_bars` bars preceding the window. Score >= 0.0; 1.0
        # means "at-or-above prior baseline".
        vol_score = _volume_accumulation_score(vols, cur_start, end_idx)

        cz = ConsolidationZone(
            active=True,
            floor=floor_v,
            ceiling=ceiling_v,
            duration_bars=duration,
            range_pct_of_ma200=range_pct,
            ma_convergence_score=ma_score,
            volume_accumulation_score=vol_score,
            reason="ok",
        )

        # Prefer the longest qualifying window.
        if best is None or cz.duration_bars > best.duration_bars:
            best = cz

    if best is None:
        return ConsolidationZone(False, reason="no_qualifying_window")
    return best


def _ma_convergence_score(
    closes: list[float], idx: int,
) -> Optional[float]:
    """Computes how tightly MA5/10/20/50 cluster around their mean at
    `idx`. Score = 1 - (max_spread / mean), clipped to [0, 1].
    None when any MA hasn't warmed up. Tighter ≈ closer to 1.
    """
    if idx < 49:
        return None
    candidates = []
    for period in (5, 10, 20, 50):
        if idx + 1 < period:
            return None
        sub = closes[idx + 1 - period: idx + 1]
        candidates.append(sum(sub) / period)
    if not candidates:
        return None
    mean = sum(candidates) / len(candidates)
    if mean <= 0:
        return None
    spread = (max(candidates) - min(candidates)) / mean
    score = 1.0 - (spread / Config.ma_convergence_max_pct)
    return max(0.0, min(1.0, score))


def _volume_accumulation_score(
    vols: list[float], start: int, end: int,
) -> Optional[float]:
    """Mean window volume / mean prior-window baseline. Clipped at 2.0
    then normalized to [0, 1]. None if there isn't enough prior data.
    """
    window_n = end - start + 1
    if window_n <= 0:
        return None
    window_mean = sum(vols[start: end + 1]) / window_n

    prior_start = max(0, start - window_n)
    prior_n = start - prior_start
    if prior_n <= 0:
        return None
    prior_mean = sum(vols[prior_start: start]) / prior_n
    if prior_mean <= 0:
        return None

    ratio = window_mean / prior_mean
    return max(0.0, min(1.0, ratio / 2.0))


def _detect_breakout(
    closes: list[float], vols: list[float], consol: ConsolidationZone,
) -> ExplosiveBreakout:
    """Did the last K bars close above the consolidation ceiling on
    a volume surge? Requires `consol.active=True`."""
    if not consol.active or consol.ceiling is None:
        return ExplosiveBreakout(False, reason="no_consolidation")

    n = len(closes)
    if n < 21:  # need ≥ 20 bars for the breakout-volume baseline
        return ExplosiveBreakout(False, reason="insufficient_bars_for_volume_baseline")

    window = Config.breakout_window_bars
    ceiling = consol.ceiling
    mult = Config.breakout_ceiling_mult
    vol_mult = Config.breakout_volume_mult

    # Scan the last `window` bars newest-first.
    for offset, idx in enumerate(range(n - 1, max(n - 1 - window, -1), -1)):
        close_i = closes[idx]
        if close_i < ceiling * mult:
            continue
        # Volume reference: prior 20 bars BEFORE this candidate bar.
        ref_start = max(0, idx - 20)
        if idx - ref_start < 20:
            continue
        vol_ref = sum(vols[ref_start: idx]) / 20.0
        if vol_ref <= 0:
            continue
        vol_ratio = vols[idx] / vol_ref
        if vol_ratio < vol_mult:
            continue
        breakout_pct = (close_i - ceiling) / ceiling
        return ExplosiveBreakout(
            active=True,
            breakout_pct=breakout_pct,
            volume_surge_multiple=vol_ratio,
            bars_since_breakout=offset,
            ceiling_referenced=ceiling,
            reason="ok",
        )
    return ExplosiveBreakout(False, reason="no_breakout_in_window")


def _composite_score(
    ma: MA200Uptrend, cz: ConsolidationZone, bo: ExplosiveBreakout,
) -> float:
    """Descriptive [0, 1] score blending the three signals.

    Weights (operator-tunable later if needed):
      - MA200 uptrend: 0.30 (gate-like — anchor of the setup)
      - Consolidation: 0.40 (the body of the pattern)
        * 0.15 base "active"
        * 0.15 × ma_convergence_score
        * 0.10 × volume_accumulation_score
      - Breakout: 0.30
        * 0.20 base "active"
        * 0.10 × min(breakout_pct/0.05, 1) AND min(vol/2.5, 1) avg
    """
    score = 0.0
    if ma.active:
        score += 0.30
    if cz.active:
        score += 0.15
        if cz.ma_convergence_score is not None:
            score += 0.15 * cz.ma_convergence_score
        if cz.volume_accumulation_score is not None:
            score += 0.10 * cz.volume_accumulation_score
    if bo.active:
        score += 0.20
        bp = bo.breakout_pct or 0.0
        vs = bo.volume_surge_multiple or 0.0
        norm_bp = min(bp / 0.05, 1.0)
        norm_vs = min((vs - 1.0) / 1.5, 1.0)   # normalize vs ∈ [1.0, 2.5]
        score += 0.10 * max(0.0, (norm_bp + norm_vs) / 2.0)
    return round(min(1.0, max(0.0, score)), 4)


def _small_cap_flag(float_millions: Optional[float]) -> Optional[bool]:
    if float_millions is None:
        return None
    try:
        return float(float_millions) <= Config.small_cap_float_max_millions
    except (TypeError, ValueError):
        return None


def _config_snapshot() -> dict:
    """Snapshot the live thresholds onto each result so historical
    replays can reproduce what was true at detection time."""
    return {
        "ma200_uptrend_bars": Config.ma200_uptrend_bars,
        "consolidation_range_max_pct": Config.consolidation_range_max_pct,
        "consolidation_min_bars": Config.consolidation_min_bars,
        "ma_convergence_max_pct": Config.ma_convergence_max_pct,
        "breakout_ceiling_mult": Config.breakout_ceiling_mult,
        "breakout_volume_mult": Config.breakout_volume_mult,
        "breakout_window_bars": Config.breakout_window_bars,
        "small_cap_float_max_millions": Config.small_cap_float_max_millions,
    }
