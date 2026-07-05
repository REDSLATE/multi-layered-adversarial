"""Roster default-assignments doctrine — pinned by unit test (2026-02-28).

Doctrine (operator-pinned 2026-02-28):

    The crypto lane defaults to a WORKING mirror of equity.

Prior doctrine ("crypto starts vacant per Paradox v2") was reverted
after this concrete failure mode was observed on 2026-07-05:

    * 869 crypto intents emitted / 0 executed in a 24h window
    * Funnel `top_block_reason`: `executor_seat_vacant:crypto`
    * Root cause: every `POST /api/admin/roster/reset` (including
      operator UI clicks on "Reset to Defaults") silently disabled
      the crypto lane by re-vacating the 4 crypto seats.

If a future refactor reverts `DEFAULT_ASSIGNMENTS` to all-None for
the crypto lane, this test catches it BEFORE it lands.

The test is intentionally NON-destructive: it only imports the
module-level constant, never touches the DB. Ships in the default
`pytest` run (no `-m destructive` needed).
"""
from __future__ import annotations

import sys

sys.path.insert(0, "/app/backend")


def test_default_assignments_populate_crypto_executor():
    """The crypto executor seat MUST be filled by default. Vacating
    it means `seat.decide()` returns `verdict=pass` with
    `executor_seat_vacant:crypto` for every crypto intent — no
    execution possible. This is the top-priority invariant."""
    from shared.roster import DEFAULT_ASSIGNMENTS
    assert DEFAULT_ASSIGNMENTS["crypto"] == "camino", (
        f"crypto executor default expected 'camino'; got "
        f"{DEFAULT_ASSIGNMENTS['crypto']!r}. If this fails, "
        f"/api/admin/roster/reset will silently disable the "
        f"crypto lane on every call."
    )


def test_default_assignments_populate_crypto_strategist_and_governor():
    """The 3 non-auditor crypto seats must be filled by default.
    Crypto strategist proposes; crypto executor authorizes; crypto
    governor sizes. All three are needed for a healthy pipeline."""
    from shared.roster import DEFAULT_ASSIGNMENTS
    assert DEFAULT_ASSIGNMENTS["crypto_strategist"] == "barracuda"
    assert DEFAULT_ASSIGNMENTS["crypto_governor"] == "gto"


def test_default_assignments_auditor_seats_remain_vacant():
    """Both auditor seats intentionally start vacant — operator-
    assigned only. Mirror behavior between equity and crypto lanes."""
    from shared.roster import DEFAULT_ASSIGNMENTS
    assert DEFAULT_ASSIGNMENTS["auditor"] is None
    assert DEFAULT_ASSIGNMENTS["crypto_auditor"] is None


def test_default_assignments_uses_governor_eligible_brains_only():
    """Governor seats MUST be held by a governor-eligible brain
    (hellcat or gto per `_GOVERNOR_EXCLUSIVE_BRAINS`). If defaults
    ever try to seat barracuda or camino as governor, the eligibility
    validator will refuse the assignment and /reset will crash."""
    from shared.roster import DEFAULT_ASSIGNMENTS, _GOVERNOR_EXCLUSIVE_BRAINS
    eq_gov = DEFAULT_ASSIGNMENTS.get("governor")
    cr_gov = DEFAULT_ASSIGNMENTS.get("crypto_governor")
    if eq_gov is not None:
        assert eq_gov in _GOVERNOR_EXCLUSIVE_BRAINS, (
            f"equity governor default {eq_gov!r} is not governor-eligible"
        )
    if cr_gov is not None:
        assert cr_gov in _GOVERNOR_EXCLUSIVE_BRAINS, (
            f"crypto governor default {cr_gov!r} is not governor-eligible"
        )


def test_default_assignments_crypto_and_equity_governors_are_distinct():
    """Doctrine (2026-02-28): the two governor seats should be held
    by DIFFERENT brains by default. Splitting the risk-regime authority
    across the two governor-eligible brains keeps lane-level sizing
    decisions independent — one brain being conservative on equity
    shouldn't force the same posture onto crypto (or vice versa)."""
    from shared.roster import DEFAULT_ASSIGNMENTS
    eq_gov = DEFAULT_ASSIGNMENTS.get("governor")
    cr_gov = DEFAULT_ASSIGNMENTS.get("crypto_governor")
    if eq_gov and cr_gov:
        assert eq_gov != cr_gov, (
            f"both governor seats defaulted to {eq_gov!r}; the two "
            f"governor-eligible brains should be split across lanes "
            f"for independent risk regimes."
        )


def test_default_assignments_all_four_brains_present():
    """All 4 brains in the fleet MUST appear at least once in the
    default assignment map — otherwise a brain is silently benched
    every time /reset runs, wasting an emitter."""
    from shared.roster import DEFAULT_ASSIGNMENTS, BRAINS
    seated = {v for v in DEFAULT_ASSIGNMENTS.values() if v}
    unseated = set(BRAINS) - seated
    assert not unseated, (
        f"brains not in default map: {unseated}. Every brain should "
        f"hold at least one default seat so /reset yields a maximally-"
        f"active fleet."
    )
