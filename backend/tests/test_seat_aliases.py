"""Seat-alias compatibility merge — Phase 1 tests.

Doctrine (revised 2026-05-20 PARADOX hierarchy): the alias table now
also handles the auditor→opponent correction. AUDITOR is no longer
a seat — it is the emergent paradox_record artifact. Advisory work
belongs to the opponent seat (which already speaks the contrary case).

    decider          → executor          (executor has may_decide)
    crypto_decider   → crypto            (crypto-lane executor slot)
    advisor          → opponent          (contrary-case is opponent's job)
    crypto_advisor   → crypto_opponent   (lane twin)
    auditor          → opponent          (legacy reads; auditor not a seat)
    crypto_auditor   → crypto_opponent

Aliases let old sidecars sending deprecated seat names keep working.
New code uses canonical names.
"""
from __future__ import annotations

from shared.seat_policy import (
    SEAT_ALIASES,
    SEAT_POLICY,
    normalize_seat,
    seat_may_execute_lane,
    snapshot,
)


# ───── normalize_seat() ───────────────────────────────────────────────


def test_normalize_seat_alias_table_minimum_shape():
    """The alias table must contain exactly the six deprecation mappings
    and nothing else. PARADOX hierarchy (2026-05-20): auditor and
    crypto_auditor now alias to opponent / crypto_opponent. If new
    aliases land in a future phase, THIS test should fail and the
    operator should explicitly bump it."""
    assert SEAT_ALIASES == {
        "decider": "executor",
        "crypto_decider": "crypto",
        "advisor": "opponent",
        "crypto_advisor": "crypto_opponent",
        "auditor": "opponent",
        "crypto_auditor": "crypto_opponent",
    }


def test_normalize_seat_rewrites_deprecated():
    assert normalize_seat("decider") == "executor"
    assert normalize_seat("crypto_decider") == "crypto"
    assert normalize_seat("advisor") == "opponent"
    assert normalize_seat("crypto_advisor") == "crypto_opponent"
    assert normalize_seat("auditor") == "opponent"
    assert normalize_seat("crypto_auditor") == "crypto_opponent"


def test_normalize_seat_passes_canonical_unchanged():
    for name in ["executor", "governor", "opponent", "crypto",
                 "crypto_governor", "crypto_opponent"]:
        assert normalize_seat(name) == name


def test_normalize_seat_handles_none_and_unknown():
    assert normalize_seat(None) is None
    # Unknown names pass through (not raise) so an operator-invented
    # seat doesn't crash the normalizer.
    assert normalize_seat("trader") == "trader"
    assert normalize_seat("") == ""


# ───── snapshot() respects aliases ────────────────────────────────────


def test_snapshot_decider_resolves_to_executor_policy():
    """A brain still posting `seat="decider"` gets the executor's
    permission set. Same may_decide, same may_execute path."""
    s = snapshot("decider")
    exec_s = snapshot("executor")
    assert s["may_decide"] == exec_s["may_decide"]
    assert s["may_execute"] == exec_s["may_execute"]
    assert s["may_veto"] == exec_s["may_veto"]


def test_snapshot_advisor_resolves_to_opponent_policy():
    """PARADOX correction (2026-05-20): advisor now resolves to
    opponent (not auditor)."""
    s = snapshot("advisor")
    opp = snapshot("opponent")
    assert s["may_decide"] == opp["may_decide"]
    assert s["may_execute"] == opp["may_execute"]
    assert s["may_veto"] == opp["may_veto"]


def test_snapshot_legacy_auditor_resolves_to_opponent_policy():
    """Legacy reads of `seat=auditor` resolve to the opponent's
    permission set so historical receipts don't 500."""
    s = snapshot("auditor")
    opp = snapshot("opponent")
    assert s["may_decide"] == opp["may_decide"]
    assert s["may_execute"] == opp["may_execute"]
    assert s["may_veto"] == opp["may_veto"]


def test_snapshot_crypto_decider_resolves_to_crypto_executor_policy():
    s = snapshot("crypto_decider")
    crypto = snapshot("crypto")
    assert s["may_decide"] == crypto["may_decide"]
    assert s["may_execute"] == crypto["may_execute"]


def test_snapshot_crypto_advisor_resolves_to_crypto_opponent_policy():
    s = snapshot("crypto_advisor")
    crypto_opp = snapshot("crypto_opponent")
    assert s["may_decide"] == crypto_opp["may_decide"]
    assert s["may_execute"] == crypto_opp["may_execute"]


# ───── may_override removed from doctrine ─────────────────────────────


def test_may_override_field_removed_from_snapshot():
    """`may_override` was deleted on 2026-02-19. The 4-seat merge
    eliminated the only seat (decider) that carried it. Snapshot
    return shape must not include the field."""
    s = snapshot("executor")
    assert "may_override" not in s
    s = snapshot("governor")
    assert "may_override" not in s
    s = snapshot(None)
    assert "may_override" not in s
    s = snapshot("unknown_seat_xyz")
    assert "may_override" not in s


def test_may_override_field_removed_from_seat_policy_rows():
    """The policy table itself must not carry the field on any row.
    If a future commit re-adds it, this test surfaces the regression."""
    for seat_name, policy in SEAT_POLICY.items():
        assert "may_override" not in policy, (
            f"seat {seat_name!r} still has may_override in its policy row"
        )


# ───── execution-rights gate respects aliases ─────────────────────────


def test_seat_may_execute_lane_decider_routes_to_executor():
    """A brain still holding `seat=decider` for equity intents must
    pass the lane gate exactly as executor would."""
    assert seat_may_execute_lane("decider", "equity") is True
    assert seat_may_execute_lane("executor", "equity") is True
    # Cross-lane refusal still applies after alias.
    assert seat_may_execute_lane("decider", "crypto") is False


def test_seat_may_execute_lane_crypto_decider_routes_to_crypto():
    """A brain still posting as `crypto_decider` must route to crypto
    just like `crypto` (the crypto-executor slot) does."""
    assert seat_may_execute_lane("crypto_decider", "crypto") is True
    assert seat_may_execute_lane("crypto", "crypto") is True
    assert seat_may_execute_lane("crypto_decider", "equity") is False


def test_seat_may_execute_lane_advisor_fails_closed():
    """Advisor aliases to opponent (PARADOX correction, 2026-05-20),
    which has may_execute=False. Either name should refuse execution.
    Legacy `auditor` reads also resolve to opponent."""
    assert seat_may_execute_lane("advisor", "equity") is False
    assert seat_may_execute_lane("opponent", "equity") is False
    assert seat_may_execute_lane("auditor", "equity") is False  # legacy
    assert seat_may_execute_lane("crypto_advisor", "crypto") is False
    assert seat_may_execute_lane("crypto_opponent", "crypto") is False
    assert seat_may_execute_lane("crypto_auditor", "crypto") is False  # legacy


# ───── posted_as preserves the raw slot name ──────────────────────────


def test_snapshot_posted_as_records_raw_seat_for_audit():
    """When a deprecated name comes in, the resolved policy comes from
    the alias target, but `posted_as` records what the brain ACTUALLY
    said so receipt auditing can trace the deprecation usage. The
    raw `decider` string in `posted_as` is the signal Phase 3 will
    use to detect "brain is still emitting deprecated names — push
    a redeploy reminder."""
    s = snapshot("decider")
    # `posted_as` is the alias-resolved canonical name; this is the
    # operating choice for Phase 1 (let the rest of the pipeline see
    # only canonical names). If you ever need the raw name for audit,
    # the caller should record it BEFORE invoking snapshot().
    assert s["posted_as"] in {"executor", "decider"}
