"""Crypto-lane council policy.

Doctrine (2026-02-16):
    Lifted out of `shared/council.py` so the crypto governance knobs
    live in a dedicated file. The dispatcher in `shared/council.py`
    still picks the right policy per intent (via `_policy_for_lane`)
    — but tuning crypto-specific risk shaping (size multipliers,
    opponent influence, momentum weighting) now happens HERE and only
    here. Equity policy stays in `equity_policy.py`.

    File-organization invariant: a crypto-only change should require
    editing zero files in the equity tree, and vice versa. This module
    is one half of that contract.
"""
from __future__ import annotations


# Crypto policy:
#   * Governance damping is reduced — soft dissent is a "risk shaper",
#     not a brake. Crypto markets punish hesitation, so we let live
#     signals through with smaller penalties than equity.
#   * Opponent influence is higher — crypto crashes are real and REDEYE
#     gets more weight when it leans the other way.
#   * Min executor confidence floor is lower (0.45 vs equity 0.50)
#     because crypto signals are noisier by nature.
#   * MAX_DOWNWEIGHT is 0.75 (vs equity 0.60) — composed downweights
#     cannot drop size below 75% of the requested notional in crypto.
#   * MOMENTUM_WEIGHTING is 1.20 — crypto rewards faster adaptation.
CRYPTO_POLICY: dict = {
    "GOVERNOR_HARD_VETO_THRESHOLD": 0.85,
    "GOVERNOR_DISSENT_CONF_MULT":   0.90,
    "GOVERNOR_DISSENT_SIZE_MULT":   0.83,
    "GOVERNOR_NO_STANCE_SIZE_MULT": 0.80,
    "GOVERNOR_NO_STANCE_CONF_MULT": 0.92,
    "OPPONENT_INFLUENCE":           0.85,    # crypto crashes are real; listen more to REDEYE
    "MIN_EXECUTOR_CONF_FLOOR":      0.45,    # slightly lower floor (crypto = noisier)
    "MAX_UPWEIGHT":                 1.25,
    # CRYPTO_GOVERNOR_DOWNWEIGHT_FLOOR (2026-02-15): tune the
    # governor lighter for crypto. Composed downweights cannot drop
    # size below 0.75 in the crypto lane — vs equity's 0.60 — so
    # crypto governance shapes risk without throttling the lane.
    "MAX_DOWNWEIGHT":               0.75,
    "MAX_SINGLE_AGENT_INFLUENCE":   0.40,
    "MOMENTUM_WEIGHTING":           1.20,    # crypto punishes hesitation — lift momentum
    # Seat-bound stack weights — apply to whoever holds the seat,
    # NOT to a brain identity. Used in governance ledger for scoring.
    #
    # Canonical 4-seat doctrine (2026-02-19): executor + governor +
    # opponent + auditor (crypto twin uses `crypto` for the executor
    # slot). The deprecated `decider` / `advisor` keys are kept in
    # this table so lookups against old stance records resolve to a
    # sensible weight during the deprecation window — they will be
    # rewritten via `SEAT_ALIASES` at the lookup boundary in a future
    # phase. New stances should never write these names.
    "STACK_WEIGHTS": {
        "executor": 1.00,
        "governor": 0.65,
        "opponent": 0.80,
        "auditor":  0.50,          # post-trade lagging signal
        "crypto":   1.00,
        # ---- deprecated, alias-rewritten on read ----
        "decider":  0.90,           # → executor; legacy stance records
        "advisor":  0.50,           # → auditor; legacy stance records
    },
}
