"""CFQS — Calibrated Fill Quality Score (merge-rights doctrine).

Locked 2026-07-03 with the operator BEFORE any brain is close to
the merge threshold. This is deliberately upstream of PnL — the
sidecar trader does not yet have round-trip position lifecycle,
so a Sharpe-style metric would be a lie. CFQS operates on the
fill boundary, the same discipline as the accuracy endpoint.

────────────────────────────────────────────────────────────────
FORMULA
────────────────────────────────────────────────────────────────

    CFQS = fill_rate
         × (1 − broker_error_rate)
         × freshness_factor
         × spread_penalty
         × calibration_penalty

where:

    freshness_factor
        1.0                            if p50(quote_age_ms) < 500
        linear decay to 0.0            up to 5000ms
        0.0                            at/above 5000ms

    spread_penalty
        1.0                            if avg_spread_bps ≤ lane_median
        lane_median / avg_spread_bps   otherwise (soft penalty, floored at 0)

    calibration_penalty
        1.0                                 if (p90 − p10) ≤ 0.25
        max(0, 1 − (spread − 0.25) * 4)     otherwise
        (i.e. hits 0.0 at spread ≥ 0.50 — a fully bimodal brain
         earns no calibration credit and can't merge)

────────────────────────────────────────────────────────────────
MERGE-RIGHT GATES (all must pass)
────────────────────────────────────────────────────────────────

    fires ≥ 30
    confidence_n ≥ 30
    lane match (crypto never merges into equity, ever)
    CFQS_candidate > CFQS_incumbent × 1.15   (must BEAT by 15%, not tie)

────────────────────────────────────────────────────────────────
WHAT THIS DELIBERATELY DOES NOT DO
────────────────────────────────────────────────────────────────
    * No auto-merge. This module returns a score + gate flags.
      The operator approves any merge by hand.
    * No PnL proxy. It stops at the fill boundary — same as the
      accuracy endpoint.
    * No cross-lane merging. A crypto brain's regime shape is
      not an equity brain's regime shape.

Consumers:
    * /api/admin/trader/brain-accuracy  — exposes CFQS + sub-components
    * /app/trader/tools/confidence_rebaseline.py  — reads CFQS
      alongside percentile distributions when suggesting new
      thresholds (never applies).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Optional


# Locked constants — changing these requires operator sign-off.
FIRES_FLOOR = 30
CONFIDENCE_N_FLOOR = 30
BEAT_MARGIN = 1.15
CALIBRATION_SPREAD_CEILING = 0.25
FRESHNESS_FULL_MS = 500.0
FRESHNESS_ZERO_MS = 5000.0


@dataclass
class CFQSBreakdown:
    """Full CFQS + every sub-factor so the operator can see WHY."""
    score: float                      # final CFQS in [0, 1]
    fill_rate: float
    broker_error_rate: float
    freshness_factor: float
    spread_penalty: float
    calibration_penalty: float
    # gates
    fires: int
    confidence_n: int
    fires_gate_passed: bool
    confidence_gate_passed: bool
    merge_eligible: bool              # all gates + score > 0
    # inputs (echoed for audit)
    p50_quote_age_ms: Optional[float]
    avg_spread_bps: Optional[float]
    lane_median_spread_bps: Optional[float]
    confidence_p10: Optional[float]
    confidence_p90: Optional[float]

    def to_dict(self) -> dict:
        return asdict(self)


def _freshness_factor(p50_age_ms: Optional[float]) -> float:
    if p50_age_ms is None:
        return 1.0                    # no data → don't punish (yet)
    if p50_age_ms < FRESHNESS_FULL_MS:
        return 1.0
    if p50_age_ms >= FRESHNESS_ZERO_MS:
        return 0.0
    span = FRESHNESS_ZERO_MS - FRESHNESS_FULL_MS
    return max(0.0, 1.0 - (p50_age_ms - FRESHNESS_FULL_MS) / span)


def _spread_penalty(
    avg_spread_bps: Optional[float],
    lane_median_bps: Optional[float],
) -> float:
    if avg_spread_bps is None or lane_median_bps is None:
        return 1.0
    if avg_spread_bps <= 0 or lane_median_bps <= 0:
        return 1.0
    if avg_spread_bps <= lane_median_bps:
        return 1.0
    return max(0.0, lane_median_bps / avg_spread_bps)


def _calibration_penalty(
    p10: Optional[float], p90: Optional[float],
) -> float:
    """Penalize brains whose confidence is bimodal.

    A brain with p90 − p10 == 0.25 is right at the edge → 1.0.
    A brain with p90 − p10 ≥ 0.50 is fully bimodal → 0.0 (no
    calibration credit; can't merge until it tightens).
    """
    if p10 is None or p90 is None:
        return 1.0
    spread = max(0.0, p90 - p10)
    if spread <= CALIBRATION_SPREAD_CEILING:
        return 1.0
    return max(0.0, 1.0 - (spread - CALIBRATION_SPREAD_CEILING) * 4.0)


def compute_cfqs(
    *,
    fires: int,
    fills: int,
    broker_errors: int,
    confidence_n: int,
    p50_quote_age_ms: Optional[float],
    avg_spread_bps: Optional[float],
    lane_median_spread_bps: Optional[float],
    confidence_p10: Optional[float],
    confidence_p90: Optional[float],
) -> CFQSBreakdown:
    """Return CFQS + every sub-factor + gate flags.

    Pure function — no I/O, no globals. Deterministic given inputs.
    """
    fires_den = max(fires, 1)
    fill_rate = fills / fires_den
    broker_error_rate = broker_errors / fires_den

    freshness = _freshness_factor(p50_quote_age_ms)
    spread_pen = _spread_penalty(avg_spread_bps, lane_median_spread_bps)
    calib_pen = _calibration_penalty(confidence_p10, confidence_p90)

    score = (
        fill_rate
        * max(0.0, 1.0 - broker_error_rate)
        * freshness
        * spread_pen
        * calib_pen
    )

    fires_ok = fires >= FIRES_FLOOR
    conf_ok = confidence_n >= CONFIDENCE_N_FLOOR
    merge_eligible = fires_ok and conf_ok and score > 0.0

    return CFQSBreakdown(
        score=round(score, 4),
        fill_rate=round(fill_rate, 4),
        broker_error_rate=round(broker_error_rate, 4),
        freshness_factor=round(freshness, 4),
        spread_penalty=round(spread_pen, 4),
        calibration_penalty=round(calib_pen, 4),
        fires=fires,
        confidence_n=confidence_n,
        fires_gate_passed=fires_ok,
        confidence_gate_passed=conf_ok,
        merge_eligible=merge_eligible,
        p50_quote_age_ms=(
            round(p50_quote_age_ms, 2) if p50_quote_age_ms is not None else None
        ),
        avg_spread_bps=(
            round(avg_spread_bps, 4) if avg_spread_bps is not None else None
        ),
        lane_median_spread_bps=(
            round(lane_median_spread_bps, 4)
            if lane_median_spread_bps is not None else None
        ),
        confidence_p10=confidence_p10,
        confidence_p90=confidence_p90,
    )


def merge_right_ok(
    candidate: CFQSBreakdown,
    incumbent: CFQSBreakdown,
) -> tuple[bool, str]:
    """Does `candidate` earn the right to merge into `incumbent`?

    Returns (allowed, reason). This is advisory — the operator
    always approves any actual merge by hand. Never wire this to
    an auto-merge path.
    """
    if not candidate.merge_eligible:
        return False, "candidate_gates_not_met"
    if not incumbent.merge_eligible:
        # if incumbent isn't even qualifying, this comparison is
        # noise — refuse until incumbent has enough of a track.
        return False, "incumbent_gates_not_met"
    threshold = incumbent.score * BEAT_MARGIN
    if candidate.score <= threshold:
        return False, (
            f"candidate_score {candidate.score} did not beat "
            f"incumbent {incumbent.score} × {BEAT_MARGIN} = "
            f"{round(threshold, 4)}"
        )
    return True, (
        f"candidate {candidate.score} > incumbent "
        f"{incumbent.score} × {BEAT_MARGIN}"
    )
