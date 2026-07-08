"""Per-brain strategy identity — content hash of `strategy.py`.

Doctrine pin (2026-02-19, operator directive):
    The status endpoint's identity block currently shows `git_sha`
    for every brain — correctly SHARED (same repo, same deploy),
    but misleading because it's the ONLY identity field and looks
    like it should differentiate. The actually-different thing —
    which strategy code each brain is running — was not surfaced.

    `strategy_sha` fixes that by hashing the physical
    `shared/brains/<brain>/strategy.py` file. If two brains ever
    show the same `strategy_sha`, that's a real bug: their strategy
    files have converged (bad refactor, accidental symlink, wrong
    import). The boot-time collision assertion turns that into a
    loud fail instead of a silent behavior drift.

    Kept as its own module — one hash function, one location — so
    other identity paths (audit log, telemetry, brain-input-health)
    can call the same helper without duplicating the file resolution
    or coping with a stale cache.
"""
from __future__ import annotations

import hashlib
import pathlib
from typing import Dict, Iterable

# Compute once per interpreter — strategy.py contents are immutable
# during the process lifetime (hot reload rewrites the file, but
# the module then also gets re-imported which restarts the whole
# process in this deployment; no need to invalidate).
_STRATEGY_SHA_CACHE: Dict[str, str] = {}


def _strategy_path(brain_id: str) -> pathlib.Path:
    """Resolve `shared/brains/<brain_id>/strategy.py` from anywhere.

    Uses this file's own location as the anchor so the resolution
    doesn't depend on the CWD.
    """
    # this file → shared/brains/_strategy_identity.py
    # target   → shared/brains/<brain_id>/strategy.py
    return pathlib.Path(__file__).parent / brain_id / "strategy.py"


def strategy_sha(brain_id: str) -> str:
    """Return a 12-char sha256 prefix of the brain's `strategy.py`.

    Cached per brain_id after the first call. Returns the sentinel
    string `"missing"` if the file doesn't exist (won't raise — the
    status endpoint must stay resilient even if a brain's strategy
    file is somehow absent).
    """
    cached = _STRATEGY_SHA_CACHE.get(brain_id)
    if cached is not None:
        return cached
    path = _strategy_path(brain_id)
    if not path.exists():
        _STRATEGY_SHA_CACHE[brain_id] = "missing"
        return "missing"
    digest = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
    _STRATEGY_SHA_CACHE[brain_id] = digest
    return digest


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
    seen: Dict[str, str] = {}
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
