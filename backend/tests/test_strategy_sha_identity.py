"""Per-brain STRATEGY_SHA identity tripwires (2026-02-19).

Doctrine pin: the status-endpoint identity block conflated two
different kinds of identity — `git_sha` (deploy, shared) vs the
actually-different-per-brain strategy code. `strategy_sha` fixes
that. Boot-time collision assertion turns "two brains' strategy
files accidentally converge" from a silent behavior drift into a
loud fail.
"""
from __future__ import annotations

import pytest

from shared.brains._strategy_identity import (
    _STRATEGY_SHA_CACHE,
    assert_no_strategy_collisions,
    strategy_sha,
)


_BRAINS = ("camino", "barracuda", "hellcat", "gto")


def setup_function(_):
    """Clear the cache before each test so a stale entry doesn't
    hide a real change to strategy.py file contents mid-suite."""
    _STRATEGY_SHA_CACHE.clear()


@pytest.mark.tripwire
def test_strategy_sha_returns_12_char_hex_for_each_brain():
    for b in _BRAINS:
        sha = strategy_sha(b)
        assert sha != "missing", f"{b}/strategy.py not found"
        assert len(sha) == 12, f"{b}: {sha!r}"
        # Must be hex characters (lowercase sha256 prefix).
        int(sha, 16)  # raises if non-hex


@pytest.mark.tripwire
def test_strategy_sha_is_stable_within_process():
    """Repeated calls MUST return the same value (cached)."""
    a = strategy_sha("camino")
    b = strategy_sha("camino")
    assert a == b


@pytest.mark.tripwire
def test_strategy_sha_differs_across_all_four_brains():
    """The core invariant: no two brains share a strategy_sha.
    If any two match, they're running identical strategy code —
    which is a bug the operator needs to see loud.
    """
    shas = {b: strategy_sha(b) for b in _BRAINS}
    assert len(set(shas.values())) == len(_BRAINS), (
        f"strategy_sha collision detected across brains: {shas}"
    )


@pytest.mark.tripwire
def test_missing_strategy_file_returns_sentinel_not_raise():
    """The status endpoint must stay resilient even if a brain's
    strategy file is somehow absent — return 'missing' sentinel,
    do NOT raise."""
    got = strategy_sha("nonexistent_brain_xyzzy")
    assert got == "missing"


@pytest.mark.tripwire
def test_assert_no_strategy_collisions_passes_current_roster():
    """Same invariant, wrapped as a boot-time assertion."""
    assert_no_strategy_collisions(_BRAINS)


@pytest.mark.tripwire
def test_assert_no_strategy_collisions_raises_on_duplicate():
    """Force a collision by passing the same brain twice — proves
    the assertion catches it. This is the doctrinal drift-alert
    behavior."""
    with pytest.raises(AssertionError, match="collision"):
        assert_no_strategy_collisions(("camino", "camino"))
