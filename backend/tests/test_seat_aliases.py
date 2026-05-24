"""Seat-alias compatibility merge — Phase 1 tests.

Doctrine (revised 2026-05-24 — operator renamed `decider` to
`strategist`, AUDITOR reinstated as a real seat):

    decider          → executor          (legacy compat; was previously
                                          merged into executor — kept
                                          as an alias so old receipts
                                          resolve. New code uses
                                          `strategist`, which is a
                                          distinct SEAT_POLICY row.)
    crypto_decider   → crypto            (crypto-lane executor slot)
    advisor          → opponent          (contrary-case is opponent's job)
    crypto_advisor   → crypto_opponent   (lane twin)

`auditor` / `crypto_auditor` are NO LONGER aliased — they are real
roster seats with their own SEAT_POLICY rows.

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
    """The alias table must contain exactly the four deprecation
    mappings and nothing else. 2026-05-24: auditor / crypto_auditor
    are NO LONGER aliased — they are real seats. If new aliases land
    in a future phase, THIS test should fail and the operator should
    explicitly bump it."""
    assert SEAT_ALIASES == {
        "decider": "executor",
        "crypto_decider": "crypto",
        "advisor": "opponent",
        "crypto_advisor": "crypto_opponent",
    }


def test_normalize_seat_rewrites_deprecated():
    assert normalize_seat("decider") == "executor"
    assert normalize_seat("crypto_decider") == "crypto"
    assert normalize_seat("advisor") == "opponent"
    assert normalize_seat("crypto_advisor") == "crypto_opponent"
    # auditor / crypto_auditor pass through unchanged — they are real seats.
    assert normalize_seat("auditor") == "auditor"
    assert normalize_seat("crypto_auditor") == "crypto_auditor"


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


def test_snapshot_auditor_resolves_to_real_auditor_policy():
    """2026-05-24: auditor is a real seat with its own policy row.
    Snapshot must NOT route it through opponent anymore."""
    s = snapshot("auditor")
    assert s["may_decide"] is False
    assert s["may_execute"] is False
    assert s["may_veto"] is False
    assert s["posted_as"] == "auditor"


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
    Auditor (real seat as of 2026-05-24) also has may_execute=False."""
    assert seat_may_execute_lane("advisor", "equity") is False
    assert seat_may_execute_lane("opponent", "equity") is False
    assert seat_may_execute_lane("auditor", "equity") is False
    assert seat_may_execute_lane("crypto_advisor", "crypto") is False
    assert seat_may_execute_lane("crypto_opponent", "crypto") is False
    assert seat_may_execute_lane("crypto_auditor", "crypto") is False


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
