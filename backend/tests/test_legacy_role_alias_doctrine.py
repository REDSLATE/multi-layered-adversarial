"""Legacy role alias rewrite layer — DOCTRINE LOCK.

History (2026-02-17): operator's P3 "cleanup" backlog item included
"Strip legacy `decider` paths in roster.py". A DB safety check found
25% of `sovereign_audit_log` rows still carry the `decider` /
`crypto_decider` keys from before the 2026-05-24 rename. The alias-
rewrite layer is LOAD-BEARING for historical audit reads.

This tripwire locks the doctrine pin so a future agent's "cleanup"
attempt trips the test bar before merge. The aliases may only be
removed AFTER a one-shot DB migration backfills canonical keys across
every collection that ever stored a role/seat/posted_as field.
"""
from __future__ import annotations

import inspect

import pytest

from shared import roster as r


pytestmark = [pytest.mark.tripwire]


def test_legacy_role_rewrites_dict_exists():
    """The alias dict must exist."""
    assert hasattr(r, "_LEGACY_ROLE_REWRITES"), (
        "shared.roster._LEGACY_ROLE_REWRITES has been deleted. This "
        "table translates historical role names ('decider', "
        "'crypto_decider', 'opponent', 'crypto_opponent') to their "
        "canonical replacements on read. Removing it corrupts ~25%% "
        "of the sovereign_audit_log on read. Read the doctrine pin "
        "above the dict definition before touching this."
    )


def test_decider_aliases_preserved():
    """The `decider` → `strategist` rewrite is the load-bearing one;
    1,363 sovereign_audit_log rows depend on it as of the audit
    on 2026-02-17."""
    table = r._LEGACY_ROLE_REWRITES
    assert table.get("decider") == "strategist", (
        "`decider` alias rewrite missing or wrong. This was the "
        "primary historical seat name before 2026-05-24."
    )
    assert table.get("crypto_decider") == "crypto_strategist", (
        "`crypto_decider` alias rewrite missing or wrong."
    )


def test_opponent_aliases_preserved():
    """`opponent` → `auditor` rewrite (2026-05-27)."""
    table = r._LEGACY_ROLE_REWRITES
    assert table.get("opponent") == "auditor"
    assert table.get("crypto_opponent") == "crypto_auditor"


def test_canonical_role_helper_uses_table():
    """`_canonical_role` must consult the alias table — not hardcode
    its own translations. Otherwise the rewrite path forks."""
    src = inspect.getsource(r._canonical_role)
    assert "_LEGACY_ROLE_REWRITES" in src, (
        "_canonical_role must read from _LEGACY_ROLE_REWRITES rather "
        "than hardcoding aliases. Otherwise the alias table can drift "
        "from actual rewrite behavior."
    )


def test_canonical_role_passes_unknown_through():
    """Unknown / canonical role names must be returned unchanged."""
    assert r._canonical_role("strategist") == "strategist"
    assert r._canonical_role("governor") == "governor"
    assert r._canonical_role("brand_new_seat") == "brand_new_seat"


def test_doctrine_pin_present_in_source():
    """The doctrine note explaining WHY these aliases are mandatory
    must remain above the dict — operator-readable warning against
    a 'just delete it' regression."""
    src = inspect.getsource(r)
    assert "DOCTRINE PIN" in src
    assert "DO NOT REMOVE" in src
    assert "sovereign_audit_log" in src, (
        "Doctrine note must name the specific collection at risk "
        "so a future agent knows why the cleanup is unsafe."
    )
