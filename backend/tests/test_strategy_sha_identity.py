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
    assert_no_strategy_collisions,
    strategy_sha,
)


_BRAINS = ("camino", "barracuda", "hellcat", "gto")


@pytest.mark.tripwire
def test_strategy_sha_returns_12_char_hex_for_each_brain():
    for b in _BRAINS:
        sha = strategy_sha(b)
        assert sha != "missing", f"{b}/strategy.py not found"
        assert len(sha) == 12, f"{b}: {sha!r}"
        int(sha, 16)  # must be lowercase hex


@pytest.mark.tripwire
def test_strategy_sha_is_deterministic_across_calls():
    """Same file content → same hash. This is a property of sha256
    over an unchanged file, not of any caching layer — the helper
    is deliberately uncached so this determinism comes purely from
    file-content stability, not from memoization."""
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


@pytest.mark.tripwire
def test_strategy_sha_reflects_file_content_change_immediately(tmp_path, monkeypatch):
    """The whole reason the cache was stripped: an edit to
    `strategy.py` must be reflected in `strategy_sha` on the very
    next call, with no restart or cache-flush required. This test
    pins that invariant so a future perf-minded refactor can't
    silently reintroduce caching without breaking this contract.
    """
    # Point `_strategy_path` at a temp file we control.
    from shared.brains import _strategy_identity as mod

    fake = tmp_path / "strategy.py"
    fake.write_text("# initial\n")
    monkeypatch.setattr(mod, "_strategy_path", lambda _b: fake)

    before = strategy_sha("any_brain")
    fake.write_text("# mutated\n")
    after = strategy_sha("any_brain")

    assert before != after, (
        "strategy_sha did not change after strategy.py edit — a "
        "cache has been reintroduced somewhere and is now stale-"
        "returning. See module docstring for why this is banned."
    )
