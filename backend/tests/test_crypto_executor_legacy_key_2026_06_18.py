"""Regression test for the `crypto_executor → crypto` legacy migration.

2026-06-18: operator on Prod filled all 8 seats via QuickSeatSwitches.
The UI showed every brain pill highlighted, but the SEAT REGISTRY DRIFT
banner kept saying "no executor assigned for lane=crypto". Root cause:
older Prod roster docs had the legacy seat key `crypto_executor`, but
`_LEGACY_ROLE_REWRITES` in `shared/roster.py` was missing that
migration entry — so `get_roster()` never rewrote the key.

Pure unit test: asserts the rewrite table contains the expected
mapping. Avoids touching mongo (the real `get_roster()` migration is
covered indirectly because it iterates the same dict).
"""
from __future__ import annotations

import sys


def test_legacy_role_rewrites_includes_crypto_executor():
    sys.path.insert(0, "/app/backend")
    from shared.roster import _LEGACY_ROLE_REWRITES

    # The four canonical rewrites we must preserve.
    expected_minimum = {
        "decider": "strategist",
        "crypto_decider": "crypto_strategist",
        "opponent": "auditor",
        "crypto_opponent": "crypto_auditor",
        # 2026-06-18 addition — see roster.py inline comment.
        "crypto_executor": "crypto",
    }
    for legacy, canonical in expected_minimum.items():
        assert _LEGACY_ROLE_REWRITES.get(legacy) == canonical, (
            f"_LEGACY_ROLE_REWRITES missing {legacy!r} → {canonical!r}. "
            f"Current table: {_LEGACY_ROLE_REWRITES}"
        )


def test_seat_aliases_and_legacy_rewrites_agree_on_crypto_executor():
    """The two alias tables (seat_policy.SEAT_ALIASES and
    roster._LEGACY_ROLE_REWRITES) MUST agree on the `crypto_executor`
    mapping, otherwise a doc written via one path won't be readable
    via the other — which is what bit us on Prod."""
    sys.path.insert(0, "/app/backend")
    from shared.roster import _LEGACY_ROLE_REWRITES
    from shared.seat_policy import SEAT_ALIASES

    assert SEAT_ALIASES["crypto_executor"] == "crypto"
    assert _LEGACY_ROLE_REWRITES["crypto_executor"] == "crypto"
    assert SEAT_ALIASES["crypto_executor"] == _LEGACY_ROLE_REWRITES["crypto_executor"]


def test_migration_logic_rewrites_legacy_key_in_dict():
    """Standalone simulation of the migration loop in get_roster().
    Verifies that the loop rewrites the legacy key correctly without
    requiring the database round-trip."""
    sys.path.insert(0, "/app/backend")
    from shared.roster import _LEGACY_ROLE_REWRITES

    # Simulate the in-memory doc the migration loop operates on.
    assignments = {
        "strategist": "camino",
        "crypto_executor": "hellcat",  # legacy
        # crypto canonical key absent — should be backfilled
    }
    for legacy, canonical in _LEGACY_ROLE_REWRITES.items():
        if legacy in assignments:
            if canonical not in assignments or assignments.get(canonical) is None:
                assignments[canonical] = assignments[legacy]
            del assignments[legacy]

    assert assignments.get("crypto") == "hellcat"
    assert "crypto_executor" not in assignments

    # Reverse case: canonical wins when both present.
    assignments2 = {
        "crypto_executor": "hellcat",
        "crypto": "barracuda",
    }
    for legacy, canonical in _LEGACY_ROLE_REWRITES.items():
        if legacy in assignments2:
            if canonical not in assignments2 or assignments2.get(canonical) is None:
                assignments2[canonical] = assignments2[legacy]
            del assignments2[legacy]

    assert assignments2.get("crypto") == "barracuda"
    assert "crypto_executor" not in assignments2
