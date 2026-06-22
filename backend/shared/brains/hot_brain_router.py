"""Hot-Brain Router (port of operator's `shared_hot_brain_router.py`,
2026-02-21).

Evidence-weighted execution bias. Classifies brains into HOT /
NEUTRAL / COLD / UNKNOWN based on recent performance, recommends
BLOCK / REDUCE / PASS_THROUGH / ELEVATE, and emits a size_multiplier
delta. Never owns execution — it advises the existing pipeline.

IMPORTANT — this module is DORMANT in this codebase today.
`execution_wiring_patch.py` is intentionally NOT applied. The
router can be exercised in tests and via observability endpoints
but it does NOT influence live sizing or blocking until the
operator explicitly wires `execute_with_hot_brain_routing(...)`
into the pipeline AND sets a runtime flag to enable it. This is
documented here so a future reader doesn't assume "the router is
imported, therefore it runs."

Faithful port — same formulas, thresholds, taxonomy. Only changes:
  * Package path → `shared/brains/hot_brain_router.py`
  * `datetime.utcnow()` → `datetime.now(timezone.utc)` so the
    decay math is timezone-aware and matches the rest of the
    codebase. The arithmetic answer is identical.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Literal


BrainState = Literal["UNKNOWN", "COLD", "NEUTRAL", "HOT"]


class RouteAction(str, Enum):
    BLOCK = "block"
    REDUCE = "reduce"
    PASS_THROUGH = "pass"
    ELEVATE = "elevate"


@dataclass(frozen=True)
class BrainPerformance:
    brain: str
    lane: str
    symbol: str
    trades: int
    win_rate: float
    avg_return_bps: float
    profit_factor: float
    max_drawdown_bps: float
    streak_wins: int
    streak_losses: int
    last_trade_at: datetime
    lane_win_rate: float
    symbol_win_rate: float


@dataclass(frozen=True)
class RouterContext:
    governor_size_mult: float
    governor_vote_required: bool
    verifier_seat_tier: Literal["probation", "standard", "senior", "locked"]
    roadguard_status: Literal["BLOCKED", "OPEN"]
    current_portfolio_heat: float


@dataclass(frozen=True)
class HotBrainDecision:
    brain: str
    lane: str
    symbol: str
    state: BrainState
    hot_score: float
    lane_adjusted_score: float
    route_action: RouteAction
    size_multiplier_delta: float
    reason: str
    overrides_governor: bool


def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def normalize_return_bps(avg_return_bps: float) -> float:
    return clamp((avg_return_bps + 100.0) / 200.0)


def normalize_profit_factor(pf: float) -> float:
    return clamp((pf - 0.5) / 1.5)


def normalize_drawdown(max_drawdown_bps: float) -> float:
    dd = abs(max_drawdown_bps)
    return clamp(1.0 - (dd / 500.0))


def normalize_streak(streak: float) -> float:
    return clamp((streak + 5) / 10.0)


def _time_decay_factor(last_trade_at: datetime,
                       half_life_days: int = 30) -> float:
    now = datetime.now(timezone.utc)
    if last_trade_at.tzinfo is None:
        last_trade_at = last_trade_at.replace(tzinfo=timezone.utc)
    days_since = (now - last_trade_at).days
    return 0.5 ** (days_since / half_life_days)


def compute_hot_score(p: BrainPerformance) -> float:
    decay = _time_decay_factor(p.last_trade_at)
    net_streak = p.streak_wins - (p.streak_losses * 1.5)
    base_score = clamp(
        (p.win_rate * 0.30)
        + (normalize_return_bps(p.avg_return_bps) * 0.20)
        + (normalize_profit_factor(p.profit_factor) * 0.20)
        + (normalize_drawdown(p.max_drawdown_bps) * 0.15)
        + (normalize_streak(net_streak) * 0.10)
        + (normalize_streak(p.lane_win_rate - p.win_rate) * 0.05)
    )
    return clamp(base_score * decay + 0.1)


def classify_brain(p: BrainPerformance) -> BrainState:
    if p.trades < 10:
        return "UNKNOWN"
    score = compute_hot_score(p)
    lane_boost = 0.08 if p.lane_win_rate > p.win_rate + 0.1 else 0.0
    adjusted = clamp(score + lane_boost)
    if adjusted >= 0.70 and p.profit_factor >= 1.25 and p.win_rate >= 0.55:
        return "HOT"
    if adjusted <= 0.35 or p.profit_factor < 0.75 or p.win_rate < 0.38:
        return "COLD"
    return "NEUTRAL"


def route_hot_brain(p: BrainPerformance, ctx: RouterContext) -> HotBrainDecision:
    state = classify_brain(p)
    score = compute_hot_score(p)
    lane_score = clamp(
        score + (0.08 if p.lane_win_rate > p.win_rate + 0.1 else 0.0)
    )

    if ctx.roadguard_status == "BLOCKED":
        return HotBrainDecision(
            brain=p.brain, lane=p.lane, symbol=p.symbol,
            state=state, hot_score=score, lane_adjusted_score=lane_score,
            route_action=RouteAction.BLOCK,
            size_multiplier_delta=0.0,
            reason="roadguard_blocked_precedence",
            overrides_governor=False,
        )
    if ctx.verifier_seat_tier == "locked":
        return HotBrainDecision(
            brain=p.brain, lane=p.lane, symbol=p.symbol,
            state=state, hot_score=score, lane_adjusted_score=lane_score,
            route_action=RouteAction.BLOCK,
            size_multiplier_delta=0.0,
            reason="verifier_seat_locked_by_pnl",
            overrides_governor=False,
        )

    heat_adjustment = 1.0 - (ctx.current_portfolio_heat * 0.5)

    if state == "HOT":
        if ctx.governor_vote_required:
            return HotBrainDecision(
                brain=p.brain, lane=p.lane, symbol=p.symbol,
                state=state, hot_score=score, lane_adjusted_score=lane_score,
                route_action=RouteAction.PASS_THROUGH,
                size_multiplier_delta=0.0,
                reason="hot_brain_governor_vote_required_no_override",
                overrides_governor=False,
            )
        return HotBrainDecision(
            brain=p.brain, lane=p.lane, symbol=p.symbol,
            state=state, hot_score=score, lane_adjusted_score=lane_score,
            route_action=RouteAction.ELEVATE,
            size_multiplier_delta=0.25 * heat_adjustment,
            reason="hot_brain_elevated_with_governor_consent",
            overrides_governor=True,
        )

    if state == "NEUTRAL":
        return HotBrainDecision(
            brain=p.brain, lane=p.lane, symbol=p.symbol,
            state=state, hot_score=score, lane_adjusted_score=lane_score,
            route_action=RouteAction.PASS_THROUGH,
            size_multiplier_delta=0.0,
            reason="brain_neutral_governor_has_control",
            overrides_governor=False,
        )

    if state == "COLD":
        if p.lane_win_rate > 0.50 and p.symbol_win_rate > 0.50:
            return HotBrainDecision(
                brain=p.brain, lane=p.lane, symbol=p.symbol,
                state=state, hot_score=score, lane_adjusted_score=lane_score,
                route_action=RouteAction.REDUCE,
                size_multiplier_delta=-0.25,
                reason="brain_cold_but_lane_symbol_favorable_reduce_only",
                overrides_governor=False,
            )
        return HotBrainDecision(
            brain=p.brain, lane=p.lane, symbol=p.symbol,
            state=state, hot_score=score, lane_adjusted_score=lane_score,
            route_action=RouteAction.BLOCK,
            size_multiplier_delta=0.0,
            reason="brain_cold_no_mitigating_factors",
            overrides_governor=False,
        )

    # UNKNOWN
    return HotBrainDecision(
        brain=p.brain, lane=p.lane, symbol=p.symbol,
        state=state, hot_score=score, lane_adjusted_score=lane_score,
        route_action=RouteAction.REDUCE,
        size_multiplier_delta=-0.50,
        reason="insufficient_verified_trades_reduced_probe",
        overrides_governor=False,
    )
