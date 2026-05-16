"""Equity-lane council policy.

Doctrine (2026-02-16):
    Mirror of `shared/crypto/council_policy.py`. Equity governance is
    consensus-first / governance-heavy — slow, deliberate, more weight
    on the governor's veto, lower momentum bias. A change here should
    require zero edits in the crypto tree.
"""
from __future__ import annotations


# Equity policy:
#   * Consensus-first / governance-heavy.
#   * Governor dissent applies meaningful drag: -18% on confidence,
#     -25% on size. This is intentional — equity is the lane where we
#     err on the side of letting governance slow us down.
#   * Opponent influence is capped at 0.70 — equity setups should not
#     be vetoed by a single contrary voice; consensus is what matters.
#   * MIN_EXECUTOR_CONF_FLOOR is 0.50 — anything below that after
#     multipliers is blocked outright.
#   * MOMENTUM_WEIGHTING is 1.00 — no momentum bias on equities.
EQUITY_POLICY: dict = {
    "GOVERNOR_HARD_VETO_THRESHOLD": 0.85,
    "GOVERNOR_DISSENT_CONF_MULT":   0.82,    # executor conf × this on dissent
    "GOVERNOR_DISSENT_SIZE_MULT":   0.75,    # order size × this on dissent
    "GOVERNOR_NO_STANCE_SIZE_MULT": 0.65,    # size when governor alive but silent on symbol
    "GOVERNOR_NO_STANCE_CONF_MULT": 0.85,    # eff-conf reduction when no stance
    "OPPONENT_INFLUENCE":           0.70,    # max % the opponent can pull the size down
    "MIN_EXECUTOR_CONF_FLOOR":      0.50,    # below this after multipliers ⇒ block
    "MAX_UPWEIGHT":                 1.25,
    "MAX_DOWNWEIGHT":               0.60,
    "MAX_SINGLE_AGENT_INFLUENCE":   0.40,    # any one agent can move size at most ±40%
    "MOMENTUM_WEIGHTING":           1.00,    # no momentum bias on equities
    # Seat-bound stack weights — apply to whoever holds the seat,
    # NOT to a brain identity. Used in governance ledger for scoring.
    "STACK_WEIGHTS": {
        "executor": 1.00, "decider": 0.90,
        "governor": 0.65, "opponent": 0.80,
        "advisor":  0.50, "crypto":   1.00,
    },
}
