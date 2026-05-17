"""Bounded Promotion Gate — expectancy-driven, seat-doctrinal.

Doctrine pin (2026-02-17, rev3 — P1):
    Promotion / retirement decisions key on `(lane, seat,
    doctrine_version)`. The SEAT carries the authority surface; the
    DOCTRINE VERSION is what graduates. Holders are metadata only.

    Headline metric is EXPECTANCY (R-normalized), not accuracy.
    A 45% / 4.5R doctrine outperforms 75% / 0.8R — accuracy alone is a
    trap. We compute four signals per slice:

      • expectancy_R    — average net R per trade
      • max_drawdown_R  — worst consecutive-loss run in R units
      • consistency     — stability of rolling 30-trade win rate
      • samples         — trades closed and outcome-joined

    Verdict bands:
      LEARNING               samples < 100
      WATCHING               samples ≥ 100, expectancy in [-0.10, +0.30)
      CANDIDATE_PROMOTION    samples ≥ 100, expectancy ≥ +0.30R,
                             max_drawdown ≤ 5R, consistency ≥ 0.55
      CANDIDATE_RETIREMENT   samples ≥ 100 AND (expectancy < -0.10R
                                                 OR max_drawdown ≥ 8R)

    THIS MODULE IS READ-ONLY. It surfaces the gate verdict — operators
    promote/retire doctrines explicitly. No live execution-flow
    influence until a doctrine is operator-promoted (future ticket).
"""
from __future__ import annotations

import statistics
from collections import defaultdict
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Query

from auth import get_current_user
from db import db
from namespaces import DOCTRINE_SIDECARS


router = APIRouter(prefix="/admin/doctrine", tags=["doctrine"])


# ─── doctrine ideal-snapshot descriptions ───────────────────────────
# Single source of truth for "what does this doctrine want?" Read by
# the frontend DoctrineHealthPanel so onboarding stays in lockstep
# with the actual seat sidecar code. Update both when a doctrine
# version changes its ideal conditions.

DOCTRINE_IDEALS = {
    "small_account_sidecar_v1": {
        "title": "Small-Account Generic",
        "summary": (
            "Quality-over-quantity day trading for sub-$25k accounts. "
            "Source: 2025 Small Account Tool Kit."
        ),
        "wants": [
            "price in $1-$20 (sweet spot $5-$10)",
            "gap ≥ 10% (≥20% preferred)",
            "relative volume ≥ 5x",
            "float ≤ 20M (ultra <10M)",
            "news catalyst present",
            "valid pullback pattern on a leading stock",
            "trading window 7-11am EST",
        ],
        "common_rejections": [
            "no news catalyst",
            "spread too wide",
            "weak market regime",
            "float above 20M",
            "pullback on non-leading stock",
        ],
    },
    "gap_and_go_v1": {
        "title": "Gap-and-Go v1",
        "summary": (
            "Breakout-or-bailout breakout strategy. "
            "Source: Warrior Technical Analysis v3 §Gap-and-Go."
        ),
        "wants": [
            "STRONG_GAPPER (≥20% gap)",
            "ULTRA_LOW_FLOAT (<10M shares)",
            "premarket high crossed OR premarket bull-flag break",
            "price above 20/50/200 EMAs on daily",
            "high relative volume",
            "tight spread",
        ],
        "common_rejections": [
            "gap too small for gap-and-go",
            "rvol insufficient for breakout",
            "no premarket breakout setup",
            "daily trend against strategy",
            "spread kills breakout-or-bailout",
        ],
    },
    "micro_pullback_v1": {
        "title": "Micro Pullback v1",
        "summary": (
            "Dip-buy on a leading momentum runner with known stop. "
            "Source: Warrior Technical Analysis v3 §Micro Pullback."
        ),
        "wants": [
            "valid pullback pattern (MICRO_PULLBACK or BULL_FLAG)",
            "entry near half/whole dollar",
            "momentum still active",
            "no nearby resistance",
            "known pullback low (stop reference)",
            "leading stock (gap or RVOL)",
        ],
        "common_rejections": [
            "pattern not a pullback",
            "entry not near half/whole dollar",
            "momentum not active",
            "pullback low unknown — no stop reference",
            "pullback on non-leading stock",
        ],
    },
    "crypto_sidecar_v1": {
        "title": "Crypto Generic",
        "summary": "Lane-isolated crypto doctrine. Liquidity + funding + regime aware.",
        "wants": [
            "liquid pair (24h volume)",
            "tight spread",
            "trend alignment",
            "neutral funding",
            "BTC regime support",
        ],
        "common_rejections": [
            "wide spread",
            "funding crowded",
            "liquidation imbalance",
            "dead volatility",
        ],
    },
}


# ─── gate thresholds (PINNED) ───────────────────────────────────────
# Documented in PRD. Tuning these is a doctrine event, not a config.
MIN_SAMPLES = 100
EXPECTANCY_PROMOTION_FLOOR = 0.30
EXPECTANCY_RETIREMENT_FLOOR = -0.10
MAX_DRAWDOWN_PROMOTION_CEIL = 5.0
MAX_DRAWDOWN_RETIREMENT_FLOOR = 8.0
CONSISTENCY_PROMOTION_FLOOR = 0.55


def _compute_expectancy_and_drawdown(pnls_in_order):
    """Compute expectancy + max consecutive-loss drawdown in R units.

    R is normalized by mean loss size (i.e., 1R ≈ a typical loss).
    Returns (expectancy_R, max_drawdown_R, win_rate, avg_win_usd,
    avg_loss_usd, sample_size).
    """
    wins = [p for p in pnls_in_order if p > 0]
    losses = [p for p in pnls_in_order if p < 0]
    n = len(pnls_in_order)
    if n == 0:
        return 0.0, 0.0, 0.0, 0.0, 0.0, 0
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss_signed = sum(losses) / len(losses) if losses else 0.0  # negative
    avg_loss_abs = abs(avg_loss_signed) or 0.01  # avoid div-by-zero
    win_rate = len(wins) / n
    loss_rate = len(losses) / n
    # Expectancy in R: win_rate × (avg_win / risk_unit) - loss_rate × 1.0
    exp_R = win_rate * (avg_win / avg_loss_abs) - loss_rate * 1.0

    # Max consecutive-loss run in R units.
    cur_loss_R = 0.0
    max_dd_R = 0.0
    for p in pnls_in_order:
        if p < 0:
            cur_loss_R += abs(p) / avg_loss_abs
            if cur_loss_R > max_dd_R:
                max_dd_R = cur_loss_R
        else:
            cur_loss_R = 0.0

    return (
        round(exp_R, 4),
        round(max_dd_R, 4),
        round(win_rate, 4),
        round(avg_win, 4),
        round(avg_loss_signed, 4),
        n,
    )


def _compute_consistency(pnls_in_order, window: int = 30):
    """Stability of rolling-window win rate. 1.0 = perfectly stable,
    0.0 = wildly oscillating. Computed as
    `1.0 - clamp(std_dev_of_windowed_wr / 0.5)`."""
    if len(pnls_in_order) < window:
        return None
    wrs = []
    for i in range(len(pnls_in_order) - window + 1):
        chunk = pnls_in_order[i:i + window]
        wins = sum(1 for p in chunk if p > 0)
        wrs.append(wins / window)
    if len(wrs) < 2:
        return None
    sd = statistics.pstdev(wrs)
    # Empirically a clean doctrine drifts ≤0.10; >0.50 is pure noise.
    score = 1.0 - min(1.0, sd / 0.5)
    return round(max(0.0, score), 4)


def _verdict(samples, expectancy_R, max_drawdown_R, consistency):
    blockers = []
    if samples < MIN_SAMPLES:
        return "LEARNING", [f"need ≥{MIN_SAMPLES} samples (have {samples})"]

    # retirement branch — fire on EITHER bad expectancy or catastrophic dd
    retire_reasons = []
    if expectancy_R < EXPECTANCY_RETIREMENT_FLOOR:
        retire_reasons.append(
            f"expectancy {expectancy_R:.2f}R < {EXPECTANCY_RETIREMENT_FLOOR}R floor"
        )
    if max_drawdown_R >= MAX_DRAWDOWN_RETIREMENT_FLOOR:
        retire_reasons.append(
            f"max_drawdown {max_drawdown_R:.2f}R ≥ {MAX_DRAWDOWN_RETIREMENT_FLOOR}R ceiling"
        )
    if retire_reasons:
        return "CANDIDATE_RETIREMENT", retire_reasons

    # promotion branch — must clear ALL three conditions
    if expectancy_R < EXPECTANCY_PROMOTION_FLOOR:
        blockers.append(
            f"expectancy {expectancy_R:.2f}R < {EXPECTANCY_PROMOTION_FLOOR}R floor"
        )
    if max_drawdown_R > MAX_DRAWDOWN_PROMOTION_CEIL:
        blockers.append(
            f"max_drawdown {max_drawdown_R:.2f}R > {MAX_DRAWDOWN_PROMOTION_CEIL}R ceil"
        )
    if consistency is None:
        blockers.append("consistency_not_yet_computable (window=30)")
    elif consistency < CONSISTENCY_PROMOTION_FLOOR:
        blockers.append(
            f"consistency {consistency:.2f} < {CONSISTENCY_PROMOTION_FLOOR} floor"
        )

    if not blockers:
        return "CANDIDATE_PROMOTION", []
    return "WATCHING", blockers


@router.get("/promotion-status")
async def promotion_status(
    lane: Optional[Literal["equity", "crypto"]] = Query(default=None),
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    """Per-(lane, doctrine_version) promotion gate state.

    Read-only. Returns a list of doctrine slices with their current
    expectancy / drawdown / consistency / sample count and the
    verdict band. Operators decide whether to act on
    CANDIDATE_PROMOTION / CANDIDATE_RETIREMENT.
    """
    q: dict = {"outcome_join": {"$exists": True}}
    if lane:
        q["lane"] = lane

    rows = await db[DOCTRINE_SIDECARS].find(
        q, {"_id": 0},
    ).sort("ts", 1).to_list(50_000)

    # Group by (lane, doctrine_version) — the canonical scoring axis.
    grouped = defaultdict(list)
    for r in rows:
        lane_key = r.get("lane") or "unknown"
        dv = r.get("doctrine_version") or "unknown"
        oj = r.get("outcome_join") or {}
        pnl = oj.get("pnl_usd")
        if not isinstance(pnl, (int, float)):
            continue
        grouped[(lane_key, dv)].append(float(pnl))

    slices = []
    for (lane_key, dv), pnls in grouped.items():
        exp_R, dd_R, wr, avg_w, avg_l, n = _compute_expectancy_and_drawdown(pnls)
        consistency = _compute_consistency(pnls)
        verdict, blockers = _verdict(n, exp_R, dd_R, consistency)
        ideal = DOCTRINE_IDEALS.get(dv, {})
        slices.append({
            "lane": lane_key,
            "doctrine_version": dv,
            "samples": n,
            "win_rate": wr,
            "expectancy_R": exp_R,
            "max_drawdown_R": dd_R,
            "consistency": consistency,
            "avg_win_usd": avg_w,
            "avg_loss_usd": avg_l,
            "verdict": verdict,
            "blockers": blockers,
            "progress_to_min_samples": round(min(1.0, n / MIN_SAMPLES), 4),
            "ideal": {
                "title": ideal.get("title", dv),
                "summary": ideal.get("summary"),
                "wants": ideal.get("wants", []),
                "common_rejections": ideal.get("common_rejections", []),
            },
        })

    # Surface known doctrines that have ZERO samples too, so the
    # frontend can render "learning · 0/100" instead of hiding them.
    seen_dvs = {s["doctrine_version"] for s in slices}
    for dv, ideal in DOCTRINE_IDEALS.items():
        if dv in seen_dvs:
            continue
        # Pick lane from doctrine version name as a default.
        lane_default = "crypto" if "crypto" in dv else "equity"
        if lane and lane != lane_default:
            continue
        slices.append({
            "lane": lane_default,
            "doctrine_version": dv,
            "samples": 0,
            "win_rate": None,
            "expectancy_R": None,
            "max_drawdown_R": None,
            "consistency": None,
            "avg_win_usd": None,
            "avg_loss_usd": None,
            "verdict": "LEARNING",
            "blockers": [f"need ≥{MIN_SAMPLES} samples (have 0)"],
            "progress_to_min_samples": 0.0,
            "ideal": {
                "title": ideal.get("title", dv),
                "summary": ideal.get("summary"),
                "wants": ideal.get("wants", []),
                "common_rejections": ideal.get("common_rejections", []),
            },
        })

    # Sort: CANDIDATE_RETIREMENT first (urgent), then CANDIDATE_PROMOTION
    # (actionable), then WATCHING, then LEARNING.
    rank = {
        "CANDIDATE_RETIREMENT": 0,
        "CANDIDATE_PROMOTION": 1,
        "WATCHING": 2,
        "LEARNING": 3,
    }
    slices.sort(key=lambda s: (rank.get(s["verdict"], 9), -(s["samples"] or 0)))

    return {
        "slices": slices,
        "thresholds": {
            "min_samples": MIN_SAMPLES,
            "expectancy_promotion_floor": EXPECTANCY_PROMOTION_FLOOR,
            "expectancy_retirement_floor": EXPECTANCY_RETIREMENT_FLOOR,
            "max_drawdown_promotion_ceiling": MAX_DRAWDOWN_PROMOTION_CEIL,
            "max_drawdown_retirement_floor": MAX_DRAWDOWN_RETIREMENT_FLOOR,
            "consistency_promotion_floor": CONSISTENCY_PROMOTION_FLOOR,
        },
        "doctrine_note": (
            "Read-only gate state. Promotion / retirement targets "
            "(lane, seat, doctrine_version) — never brain identity. "
            "Expectancy is headline; accuracy is one input among many."
        ),
        "endpoint_version": "promotion_status_v1_expectancy_driven",
    }
