"""Opponent → Auditor merge tripwires (2026-05-27).

Locks the doctrine pin from pass #16: the `opponent` seat was merged
INTO `auditor`. The auditor now carries BOTH responsibilities:
  (1) PRE-TRADE — argues the contrary case (formerly opponent)
  (2) POST-TRADE — analyzes outcome vs intent on closed positions

The merge is implemented as an alias rewrite (same pattern as
`decider → strategist`), NOT a hard rename. Legacy code that still
references `opponent` continues to function via:
  * `_LEGACY_ROLE_REWRITES` in roster.py  (roster assignment rewrites)
  * `SEAT_ALIASES` in seat_policy.py       (policy lookups)
"""
from __future__ import annotations

import pytest

from shared.roster import (
    DEFAULT_ASSIGNMENTS,
    ROLES,
    _LEGACY_ROLE_REWRITES,
    _canonical_role,
)
from shared.seat_policy import SEAT_ALIASES, SEAT_POLICY, normalize_seat, snapshot


pytestmark = [pytest.mark.tripwire]


# ──────────────────────── alias rewrites ────────────────────────


def test_opponent_alias_rewrites_to_auditor():
    """Legacy `opponent` rewrites to canonical `auditor`."""
    assert _LEGACY_ROLE_REWRITES.get("opponent") == "auditor"
    assert _canonical_role("opponent") == "auditor"


def test_crypto_opponent_alias_rewrites_to_crypto_auditor():
    """Crypto twin must mirror the equity rewrite."""
    assert _LEGACY_ROLE_REWRITES.get("crypto_opponent") == "crypto_auditor"
    assert _canonical_role("crypto_opponent") == "crypto_auditor"


def test_seat_aliases_resolve_opponent_to_auditor():
    """seat_policy.SEAT_ALIASES rewrite path must also normalize."""
    assert SEAT_ALIASES.get("opponent") == "auditor"
    assert SEAT_ALIASES.get("crypto_opponent") == "crypto_auditor"
    assert normalize_seat("opponent") == "auditor"
    assert normalize_seat("crypto_opponent") == "crypto_auditor"


# ──────────────────────── ROLES tuple ────────────────────────


def test_roles_tuple_no_longer_lists_opponent():
    """After the merge, `opponent` is NOT in the canonical role list.
    (Legacy `opponent` still resolves via alias, but it's no longer a
    primary doctrinal seat name.)"""
    assert "opponent" not in ROLES
    assert "crypto_opponent" not in ROLES


def test_roles_tuple_still_has_auditor():
    """The merge target must remain a canonical seat."""
    assert "auditor" in ROLES
    assert "crypto_auditor" in ROLES


# ──────────────────────── default assignments ────────────────────────


def test_default_assignments_drops_opponent_keys():
    """No `opponent`/`crypto_opponent` key in DEFAULT_ASSIGNMENTS."""
    assert "opponent" not in DEFAULT_ASSIGNMENTS
    assert "crypto_opponent" not in DEFAULT_ASSIGNMENTS


def test_default_assignments_keeps_auditor_keys():
    assert "auditor" in DEFAULT_ASSIGNMENTS
    assert "crypto_auditor" in DEFAULT_ASSIGNMENTS


# ──────────────────────── seat_policy absorption ────────────────────────


def test_auditor_seat_required_inherits_from_opponent():
    """Auditor must now be seat_required=True (silence on the auditor
    seat = both adversarial blindness AND no post-trade review)."""
    assert SEAT_POLICY["auditor"]["seat_required"] is True


def test_crypto_auditor_seat_required():
    """Crypto twin must also be seat_required=True."""
    assert "crypto_auditor" in SEAT_POLICY
    assert SEAT_POLICY["crypto_auditor"]["seat_required"] is True
    assert SEAT_POLICY["crypto_auditor"]["lane_scope"] == ["crypto"]


def test_auditor_lane_scope_is_general():
    """After absorbing opponent, auditor's lane_scope must be None
    (general). Per-lane scoping is delegated to the crypto_* twin."""
    assert SEAT_POLICY["auditor"]["lane_scope"] is None


def test_auditor_speaks_as_auditor_not_opponent():
    """The merged seat speaks AS auditor — not opponent."""
    assert SEAT_POLICY["auditor"]["speaks_as"] == "auditor"


def test_auditor_carries_no_execution_authority():
    """Doctrine pin: auditor never executes, decides, or vetoes."""
    p = SEAT_POLICY["auditor"]
    assert p["may_decide"] is False
    assert p["may_execute"] is False
    assert p["may_veto"] is False


def test_opponent_policy_row_retained_for_legacy_readers():
    """Legacy code reading SEAT_POLICY["opponent"] directly must keep
    working — the row mirrors auditor's permissions."""
    assert "opponent" in SEAT_POLICY
    op = SEAT_POLICY["opponent"]
    au = SEAT_POLICY["auditor"]
    assert op["may_decide"] == au["may_decide"]
    assert op["may_execute"] == au["may_execute"]
    assert op["may_veto"] == au["may_veto"]
    assert op["seat_required"] == au["seat_required"]
    # speaks_as is "auditor" on both — the merged identity
    assert op["speaks_as"] == "auditor"


def test_snapshot_for_opponent_resolves_to_auditor_policy():
    """snapshot(\"opponent\") must return auditor's permission set."""
    s = snapshot("opponent")
    a = snapshot("auditor")
    assert s["may_decide"] == a["may_decide"]
    assert s["may_execute"] == a["may_execute"]
    assert s["may_veto"] == a["may_veto"]


def test_snapshot_for_crypto_opponent_resolves_to_crypto_auditor():
    s = snapshot("crypto_opponent")
    # crypto_auditor → resolves through crypto_ stripping → auditor policy
    assert s["posted_as"] == "crypto_auditor"  # alias-rewritten
    assert s["may_decide"] is False
    assert s["may_execute"] is False
