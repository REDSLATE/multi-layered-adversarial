"""Per-brain strategy identity — content hash of `strategy.py`.

Doctrine pin (2026-02-19, operator directive):
    The status endpoint's identity block currently shows `git_sha`
    for every brain — correctly SHARED (same repo, same deploy),
    but misleading because it's the ONLY identity field and looks
    like it should differentiate. The actually-different thing —
    which strategy code each brain is running — was not surfaced.

    `strategy_sha` fixes that by hashing the physical
    `shared/brains/<brain>/strategy.py` file on every call. If two
    brains ever show the same `strategy_sha`, that's a real bug:
    their strategy files have converged (bad refactor, accidental
    symlink, wrong import). The boot-time collision assertion
    turns that into a loud fail instead of a silent behavior drift.

    Deliberately UNCACHED. The files are a few KB, hashed in
    microseconds, called at status-poll cadence — the perf win of
    caching is noise. Caching would introduce a real footgun
    during dev sessions: a hot-reload edit to `strategy.py` would
    NOT invalidate this module's cache, so the reported hash would
    silently go stale during exactly the kind of editing session
    where the operator most needs it to be trustworthy. Always-
    correct beats micro-optimized.
"""
from __future__ import annotations

import hashlib
import pathlib
from typing import Iterable


def _strategy_path(brain_id: str) -> pathlib.Path:
    """Resolve `shared/brains/<brain_id>/strategy.py` from anywhere.

    Uses this file's own location as the anchor so the resolution
    doesn't depend on the CWD.
    """
    return pathlib.Path(__file__).parent / brain_id / "strategy.py"


def strategy_sha(brain_id: str) -> str:
    """Return a 12-char sha256 prefix of the brain's `strategy.py`.

    Computed fresh on every call — no cache. See module docstring
    for the rationale (dev-loop staleness footgun outweighs the
    microsecond perf win at seconds-cadence poll traffic).

    Returns the sentinel string `"missing"` if the file doesn't
    exist — never raises, so the status endpoint stays resilient.
    """
    path = _strategy_path(brain_id)
    if not path.exists():
        return "missing"
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


def assert_no_strategy_collisions(brain_ids: Iterable[str]) -> None:
    """Raise `AssertionError` if any two brains share a strategy_sha.

    Meant to be called ONCE at boot from the FastAPI lifespan
    handler. A collision means two `strategy.py` files have converged
    to identical content (bad refactor, accidental symlink, wrong
    import) — that's a genuine bug that must fail loud, not silently
    let two "different" brains run identical strategy code.

    Passes `"missing"` sentinels through — if all brains' files are
    missing, that's a separate boot-time bug we want to surface too.
    """
    seen: dict[str, str] = {}
    for b in brain_ids:
        sha = strategy_sha(b)
        if sha in seen:
            raise AssertionError(
                f"strategy_sha collision: brain {b!r} shares "
                f"strategy_sha={sha} with brain {seen[sha]!r} — "
                f"their strategy.py files have identical content. "
                f"This means two 'different' brains would run "
                f"identical decision code. Fix the strategy files "
                f"before booting."
            )
        seen[sha] = b
