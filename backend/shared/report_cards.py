"""Brain Report Cards — aggregate labeled lessons into per-brain ×
per-setup × per-regime performance summaries.

Doctrine (2026-02-20 operator pin):
    Hellcat ETH breakdowns: win rate, avg return, profit factor,
    best market condition, worst market condition.
    GTO crypto momentum: same.
    Barracuda mean-reversion: same.

These are READ-ONLY aggregates over `shared_lessons`-shaped data
(which today is built on demand from the existing collections via
`shared.lessons.builder`). Nothing here writes to brains, gates, or
the broker — the aggregator is just summary statistics.

The "Setup Memory" feedback loop reads these report cards to adjust
brain confidence — see `shared.setup_memory.confidence_adjuster`.
The aggregator itself remains read-only.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Iterable, Optional

from shared.lessons.builder import build_lessons_bulk
from shared.lessons.schemas import Lesson


# Outcomes that count toward win-rate / profit-factor. The "pending"
# bucket is excluded because the position isn't resolved yet —
# including it would bias every recent setup toward "unknown".
_RESOLVED_OUTCOMES = {"win", "loss", "scratch", "missed", "avoided"}


def _profit_factor(wins_bps: list[float], losses_bps: list[float]) -> Optional[float]:
    gross_w = sum(wins_bps)
    gross_l = sum(abs(x) for x in losses_bps)
    if gross_l <= 0:
        return None if gross_w <= 0 else float("inf")
    return round(gross_w / gross_l, 2)


def _summarize(lessons: Iterable[Lesson]) -> dict:
    """Compute the headline KPIs over a list of lessons sharing the
    same (brain, setup, regime) bucket."""
    lessons = list(lessons)
    n = len(lessons)
    resolved = [le for le in lessons if le.outcome in _RESOLVED_OUTCOMES]
    pnls = [le.pnl_bps for le in resolved if le.pnl_bps is not None]
    wins = [le.pnl_bps for le in resolved if le.outcome == "win" and le.pnl_bps is not None]
    losses = [le.pnl_bps for le in resolved if le.outcome == "loss" and le.pnl_bps is not None]
    maes = [le.mae_bps for le in resolved if le.mae_bps is not None]
    mfes = [le.mfe_bps for le in resolved if le.mfe_bps is not None]

    win_rate = (
        round(len(wins) / len(resolved), 3) if resolved else None
    )
    avg_pnl_bps = round(sum(pnls) / len(pnls), 2) if pnls else None
    return {
        "intents_total": n,
        "intents_resolved": len(resolved),
        "wins": len(wins),
        "losses": len(losses),
        "scratches": sum(1 for le in resolved if le.outcome == "scratch"),
        "missed": sum(1 for le in resolved if le.outcome == "missed"),
        "avoided": sum(1 for le in resolved if le.outcome == "avoided"),
        "pending": sum(1 for le in lessons if le.outcome == "pending"),
        "win_rate": win_rate,
        "avg_pnl_bps": avg_pnl_bps,
        "avg_mae_bps": round(sum(maes) / len(maes), 2) if maes else None,
        "avg_mfe_bps": round(sum(mfes) / len(mfes), 2) if mfes else None,
        "profit_factor": _profit_factor(wins, losses),
        "executed_pct": (
            round(sum(1 for le in lessons if le.executed) / n, 3) if n else None
        ),
    }


def _summarize_plan_discipline(lessons: Iterable[Lesson]) -> dict:
    """Paradox v3 plan-discipline axis (PRD §4 Step 2).

    Aggregates the brain's PLAN-side cognition independent of order
    fill. Today this is informational only (no brain emits v3 yet,
    so every count under v3 buckets will read 0). Once Step 4 flips
    camino to v3 emits, this surface populates immediately and
    becomes the seed for the doctrine `plan_discipline` score.

    Doctrine pin (operator §11): action-only v2 emits are NOT
    counted as plan-discipline signal — only `intent_version == 'v3'`
    contributes. v2 rows are surfaced as `v2_legacy` for transparency.
    """
    lessons = list(lessons)
    v3 = [le for le in lessons if (le.intent_version or "v2") == "v3"]
    v2 = [le for le in lessons if (le.intent_version or "v2") != "v3"]

    # Per-plan-intent histogram across v3 lessons.
    intent_hist: dict[str, int] = {}
    stance_hist: dict[str, int] = {}
    setup_hist: dict[str, int] = {}
    for le in v3:
        if le.plan_intent:
            intent_hist[le.plan_intent] = intent_hist.get(le.plan_intent, 0) + 1
        if le.plan_stance:
            stance_hist[le.plan_stance] = stance_hist.get(le.plan_stance, 0) + 1
        if le.plan_setup:
            setup_hist[le.plan_setup] = setup_hist.get(le.plan_setup, 0) + 1

    # Wait-discipline: of the WAIT_* plans, how many had a resolved
    # outcome that matched the brain's directional call? Today this
    # is None until trigger_watcher (Step 5) starts stamping
    # `gate_state IN [trigger_fired, plan_invalidated, plan_expired]`.
    wait_plans = [le for le in v3 if (le.plan_intent or "").startswith("WAIT_")]

    return {
        "v3_lesson_count": len(v3),
        "v2_legacy_count": len(v2),
        "by_plan_intent": intent_hist,
        "by_plan_stance": stance_hist,
        "by_plan_setup": setup_hist,
        "wait_plans_observed": len(wait_plans),
        # Future fields (populated in Step 5 once trigger_watcher live):
        "wait_correct_rate":     None,
        "trigger_hit_rate":      None,
        "invalidation_hit_rate": None,
    }


async def build_report_card(
    *,
    stack: str,
    lane: Optional[str] = None,
    setup_id: Optional[str] = None,
    regime: Optional[str] = None,
    limit: int = 500,
) -> dict:
    """Generate one (brain, [lane], [setup], [regime]) report card.

    All filters are optional; supplying none returns a card across
    every lane / setup / regime for the brain. The result splits
    out per-setup AND per-regime breakdowns alongside the headline
    overall KPIs so the operator can see "Hellcat is great at
    crypto_breakdown_v1:SELL but terrible at unscored:BUY" in one
    pass.
    """
    lessons = await build_lessons_bulk(
        stack=stack,
        lane=lane,
        setup_id=setup_id,
        limit=limit,
    )
    if regime:
        lessons = [le for le in lessons if (le.regime or "?") == regime]

    by_setup: dict[str, list[Lesson]] = defaultdict(list)
    by_regime: dict[str, list[Lesson]] = defaultdict(list)
    by_symbol: dict[str, list[Lesson]] = defaultdict(list)
    for le in lessons:
        by_setup[le.setup_id or "?"].append(le)
        by_regime[le.regime or "?"].append(le)
        by_symbol[le.symbol].append(le)

    return {
        "brain": stack,
        "lane": lane,
        "setup_id_filter": setup_id,
        "regime_filter": regime,
        "window_intents": len(lessons),
        "overall": _summarize(lessons),
        "plan_discipline": _summarize_plan_discipline(lessons),
        "by_setup": {k: _summarize(v) for k, v in by_setup.items()},
        "by_regime": {k: _summarize(v) for k, v in by_regime.items()},
        "by_symbol_top": {
            k: _summarize(v)
            for k, v in sorted(by_symbol.items(),
                                key=lambda kv: len(kv[1]),
                                reverse=True)[:10]
        },
    }


async def build_setup_aggregate(
    *,
    setup_id: str,
    lane: Optional[str] = None,
    limit: int = 1_000,
) -> dict:
    """Cross-brain view of one setup — who plays it, who's good at it.

    Used by the Setup Memory adjuster (and the dashboard) to answer
    "Who is the strongest brain on crypto_breakdown_v1:SELL right
    now?" without scanning every brain card.
    """
    lessons = await build_lessons_bulk(
        lane=lane, setup_id=setup_id, limit=limit,
    )
    per_brain: dict[str, list[Lesson]] = defaultdict(list)
    for le in lessons:
        per_brain[le.stack].append(le)
    return {
        "setup_id": setup_id,
        "lane": lane,
        "window_intents": len(lessons),
        "by_brain": {k: _summarize(v) for k, v in per_brain.items()},
    }
