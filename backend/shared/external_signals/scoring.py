"""Pine signal grade → confidence mapping.

The Seat's confidence floor (`< 0.70` → LOG_ONLY, non-binding) is
the doctrine cutoff. The mapping table here is the single source of
truth that turns Pine's grading scheme into a float — when Pine
adds tiers or the operator recalibrates after a quarter of data,
it's one dict to edit.

Doctrine pins:
  * The map is asymmetric on purpose. A+ (best) is well above the
    0.70 Seat floor; B sits at the floor; C falls below. This
    enforces the rule that mediocre witnesses are advisory only.
  * `score` is a small bonus on top of the grade (Pine's raw
    score 0-15 contributes up to ±0.05). Bounded so the grade
    stays dominant — we trust Pine's grading more than its raw
    score, which can swing on noisy bars.
  * Unknown grades return 0.50 (mid-floor, below the 0.70 cutoff).
    Better to under-weight an unknown witness than over-weight it.
"""
from __future__ import annotations

from typing import Optional


# Single source of truth — one dict, one place. Operator-tunable after
# a quarter of live Pine data lands.
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

# Range of raw Pine score (operator-confirmed from sample payloads:
# `score: 11` in the demo). Above PINE_SCORE_MAX gets clamped.
PINE_SCORE_MIN = 0.0
PINE_SCORE_MAX = 15.0

# Maximum confidence shift the raw `score` is allowed to contribute
# on top of the grade. Bounded so the grade stays dominant.
SCORE_BONUS_CAP = 0.05

# Floor returned when the grade is missing or unknown. Sits below the
# Seat's 0.70 floor on purpose — unknown witnesses are advisory only.
UNKNOWN_GRADE_FLOOR = 0.50

# Hard clamps. Confidence is a probability — never escape [0, 1].
CONFIDENCE_MIN = 0.0
CONFIDENCE_MAX = 1.0


def grade_score_to_confidence(
    grade: Optional[str],
    score: Optional[float] = None,
) -> float:
    """Map a Pine `grade` (and optional raw `score`) to a confidence
    float in [0, 1].

    The grade dominates; the score is a small ±0.05 nudge. Unknown
    grades return `UNKNOWN_GRADE_FLOOR` (below the Seat's 0.70
    cutoff), so unknown witnesses are log-only by default.
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
        # Clamp to [-1, +1]
        if normalized > 1.0:
            normalized = 1.0
        elif normalized < -1.0:
            normalized = -1.0
        bonus = normalized * SCORE_BONUS_CAP

    confidence = base + bonus
    # Hard clamp — confidence is a probability.
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
