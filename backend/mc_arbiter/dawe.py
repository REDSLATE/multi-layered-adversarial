"""Day-Adaptive Weight Engine (DAWE) — weight math.

Pure functions + one small class-method helper on `DaweState`.
No I/O, no Mongo — the arbiter/grader modules are responsible for
persistence. Keeping the math side-effect-free makes it trivial to
unit-test convergence, bounds, and cold-start behavior.

Design freeze: `/app/memory/MC_SEAT_ARBITER.md` §4.
"""
from __future__ import annotations

import math
from typing import Optional

from mc_arbiter.models import DaweState

# ── Bounds (design freeze §4) ─────────────────────────────────────
WEIGHT_MIN = 0.40      # no permanent exile — a weak brain still gets a voice
WEIGHT_MAX = 1.40      # no runaway reinforcement either
COLD_START_GRADES = 5  # below this, effective_weight is neutral 1.0

# ── EWMA α values (design freeze §4) ──────────────────────────────
# α=0.30 → session moves meaningfully within a session but doesn't
# flip personality after one grade. α=0.10 → recent tracks day-over-
# day drift without erasing structural signal.
ALPHA_SESSION = 0.30
ALPHA_RECENT = 0.10

# ── Exponents (design freeze §4) ──────────────────────────────────
# 50/30/20 (not 40/25/20/15) because SessionContext primitives are
# deferred to Phase 2. Session dominates; prior is a weak anchor.
EXPONENT_SESSION = 0.50
EXPONENT_RECENT = 0.30
EXPONENT_PRIOR = 0.20


def ewma(previous: float, observation: float, alpha: float) -> float:
    """Standard exponentially-weighted moving average.

        new = α * observation + (1 - α) * previous

    Not much of a function on its own; wrapped so the two grader
    call sites (session + recent) can't disagree on the formula.
    """
    if not 0.0 <= alpha <= 1.0:
        raise ValueError(f"alpha must be in [0, 1], got {alpha}")
    return alpha * observation + (1.0 - alpha) * previous


def compute_effective_weight(state: DaweState) -> float:
    """Combine the three timescale weights into one bounded number.

    Cold-start guard: `grades_used_session < COLD_START_GRADES`
    returns 1.0 (fully neutral). This prevents the arm from being
    moved by thin data at the start of a session or after a
    prolonged pause. Once the brain has enough graded predictions,
    the geometric mean of the three timescales takes over.

    Returns a float clamped to [WEIGHT_MIN, WEIGHT_MAX].
    """
    if state.grades_used_session < COLD_START_GRADES:
        return 1.0
    # Guard against zero/negative — should never happen given the
    # grader always feeds `observed_quality ∈ [0, 1]`, but defensive
    # against manual DB edits.
    session = max(1e-6, state.session_weight)
    recent = max(1e-6, state.recent_weight)
    prior = max(1e-6, state.prior_weight)
    raw = (
        session ** EXPONENT_SESSION
        * recent ** EXPONENT_RECENT
        * prior ** EXPONENT_PRIOR
    )
    return max(WEIGHT_MIN, min(WEIGHT_MAX, raw))


def size_multiplier(effective_weight: float) -> float:
    """The sizing side of the rank-vs-size split (design freeze §4).

    rank uses `× effective`; sizing uses `× sqrt(effective)`. Softens
    the damage a temporary weak period does to position size — a
    weight of 0.49 (near the floor) still gives 0.70× sizing, not
    0.49×. Prevents the arm from shrinking to dust before the recent
    weight has a chance to catch up.
    """
    return math.sqrt(max(0.0, effective_weight))


def update_session(prev_weight: float, observed_quality: float) -> float:
    """Fold one new graded observation into `session_weight`.

    `observed_quality` is the DAWE grade in [0, 1] where 0.5 is
    neutral (see grader.py). We convert it to a *weight
    contribution* on the same [WEIGHT_MIN, WEIGHT_MAX] scale via a
    bounded linear map so the EWMA converges to a value inside the
    band:

        contribution = 0.40 + observed_quality * 1.00
                     = 0.40 (quality 0.0) → 1.40 (quality 1.0)
    """
    contribution = WEIGHT_MIN + observed_quality * (WEIGHT_MAX - WEIGHT_MIN)
    new = ewma(prev_weight, contribution, ALPHA_SESSION)
    return max(WEIGHT_MIN, min(WEIGHT_MAX, new))


def update_recent(prev_weight: float, session_average: float) -> float:
    """Fold today's session average into `recent_weight`. Called
    at session close (once/day), not per-grade. Convergence is
    slow by design (α=0.10) so a bad day doesn't erase weeks of
    real signal."""
    new = ewma(prev_weight, session_average, ALPHA_RECENT)
    return max(WEIGHT_MIN, min(WEIGHT_MAX, new))


def quality_from_signed_return(
    signed_return: float,
    expected_move: float,
) -> float:
    """Map a graded observation to a DAWE quality score in [0, 1].

    Formula (design freeze §6):

        quality = clamp(0.5 + signed_return / expected_move, 0, 1)

    Where:
      * `signed_return` = actual return with the direction sign
        applied (+return for a correct LONG, -return for a correct
        SHORT, etc.). Passed in already-signed by the grader.
      * `expected_move` = ATR-scaled expected magnitude for the
        grading horizon. A zero-or-negative value is treated as
        1e-6 to avoid divide-by-zero (in practice the grader guards
        this upstream but defensive here too).

    A trade that captured exactly the expected move in the correct
    direction scores 1.0; one that captured exactly the expected
    move in the *wrong* direction scores 0.0; a no-move day scores
    0.5 (neutral, no signal).
    """
    denom = max(1e-6, expected_move)
    raw = 0.5 + signed_return / denom
    return max(0.0, min(1.0, raw))
