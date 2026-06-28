"""Pine self-reported confidence — DISPLAY/DIAGNOSTIC ONLY.

Doctrine pin (TRIAL COURT, NOT A VOTING SYSTEM):
    This module maps Pine's self-grading (`grade`, `score`) into a
    float in [0, 1]. The float is persisted on the `ExternalSignal`
    row as `self_reported_confidence` and rendered in the diagnostics
    tile so the operator can SEE what Pine claims about itself.

    RISEDUAL DOES NOT ACT ON THIS NUMBER.

    Governor sizing comes from the Verifier-owned credibility
    ledger (`external_source_credibility.verified_alpha`), gated by
    `status=TRUSTED` AND `influence_allowed=True`. None of that
    logic lives here. This file's only job is "render Pine's
    self-grade legibly for the operator's eye."

    When Pine adds tiers or the operator recalibrates after a
    quarter of data, edit `GRADE_TO_CONFIDENCE` — it's the single
    source of truth.

Why keep the function at all if the number is non-binding?
    1. The diagnostics tile needs SOMETHING to render in the
       "Pine self-grade" column.
    2. Verifier will eventually correlate Pine's self-grade against
       realized outcomes — "does Pine's A+ actually outperform its
       B?" That study needs a stable numeric representation.
    3. The mapping is the cleanest place to centralize Pine schema
       knowledge (grade tiers, raw score range) so the rest of the
       code doesn't fan out parsing logic.
"""
from __future__ import annotations

from typing import Optional


# Single source of truth — one dict, one place. Operator-tunable
# after a quarter of live Pine data lands. Not load-bearing.
GRADE_TO_CONFIDENCE: dict[str, float] = {
    "A+": 0.90,
    "A":  0.80,
    "B+": 0.72,
    "B":  0.65,
    "C+": 0.55,
    "C":  0.50,
    "D":  0.40,
    "F":  0.25,
}

# Expected range of Pine's raw `score` (operator-confirmed from
# sample payloads: `score: 11` in the demo).
PINE_SCORE_MIN = 0.0
PINE_SCORE_MAX = 15.0

# Maximum confidence shift the raw score is allowed to contribute
# on top of the grade. Bounded so the grade stays dominant in the
# rendered diagnostic value.
SCORE_BONUS_CAP = 0.05

# Floor returned when Pine omits or supplies an unknown grade.
UNKNOWN_GRADE_FLOOR = 0.50

# Hard clamps. Even though this number is advisory, keep it in
# [0, 1] so downstream renderers don't have to special-case.
CONFIDENCE_MIN = 0.0
CONFIDENCE_MAX = 1.0


def pine_self_reported_confidence(
    grade: Optional[str],
    score: Optional[float] = None,
) -> float:
    """Map a Pine `grade` (and optional raw `score`) to a float in
    [0, 1] for the `ExternalSignal.self_reported_confidence` field.

    ADVISORY ONLY. The Seat does not act on this number. The
    Governor does not act on this number. It exists so the operator
    can SEE what Pine claims about itself in the diagnostics tile.

    The grade dominates; the score is a small ±SCORE_BONUS_CAP
    nudge. Unknown grades return UNKNOWN_GRADE_FLOOR.
    """
    base = GRADE_TO_CONFIDENCE.get(
        (grade or "").strip().upper(),
        UNKNOWN_GRADE_FLOOR,
    )

    bonus = 0.0
    if score is not None:
        # Normalize raw score to [-1, +1] around the midpoint of the
        # expected range. PINE_SCORE_MAX/2 is the neutral point.
        midpoint = (PINE_SCORE_MIN + PINE_SCORE_MAX) / 2.0
        span = max(PINE_SCORE_MAX - midpoint, 1e-9)
        normalized = (score - midpoint) / span
        if normalized > 1.0:
            normalized = 1.0
        elif normalized < -1.0:
            normalized = -1.0
        bonus = normalized * SCORE_BONUS_CAP

    confidence = base + bonus
    if confidence < CONFIDENCE_MIN:
        return CONFIDENCE_MIN
    if confidence > CONFIDENCE_MAX:
        return CONFIDENCE_MAX
    return confidence


def pine_dir_to_side(direction: str) -> str:
    """Map Pine's signed direction (`long`/`short`/`flat`) to the
    canonical witness `side` (`BUY`/`SELL`/`HOLD`).

    Strict mapping — anything outside the known set raises. The
    route layer catches and returns 400 so the operator sees the
    bad payload immediately.
    """
    mapping = {"long": "BUY", "short": "SELL", "flat": "HOLD"}
    key = (direction or "").strip().lower()
    if key not in mapping:
        raise ValueError(
            f"unknown Pine direction: {direction!r} (expected long/short/flat)"
        )
    return mapping[key]
