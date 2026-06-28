"""2026-02-25 — Lock the operator-UI-override → brain-math wiring.

Doctrine context (from the diagnostic that discovered this):
    `shared/brain_tuning_cache.py` defines `get_override(lane, key)`
    and a 30s Mongo refresher loop. The cache worked. Nothing read
    from it. All 4 brain strategies (barracuda, camino, gto, hellcat)
    bypassed the override path and read `doctrine.min_confidence`
    directly from compiled defaults.

    Net effect: the operator's UI tuning knobs were PLACEBO. The
    "less conservative" slider flipped a Mongo doc, the refresher
    pulled it into the cache, and the value died there because no
    strategy ever called `get_override`.

This regression suite locks the read path so a future refactor
can't silently revert it. Three guarantees:

  1. With an EMPTY cache, `effective_min_confidence()` returns the
     doctrine default unchanged → no behavioral drift from
     introducing the helper.
  2. With a populated cache, `effective_min_confidence()` returns
     the operator's override value → UI knob actually reaches math.
  3. Each of the 4 strategies imports the helper at module level →
     the import-side regression (someone deleting the import) shows
     up as a test failure here, not in production as a NameError.
"""
from __future__ import annotations

import pytest

from shared.brain_doctrine import DOCTRINES
from shared.brain_tuning_cache import _CACHE
from shared.brains._doctrine_overrides import (
    effective_min_confidence,
    effective_min_gap,
)


BRAINS = ("barracuda", "camino", "gto", "hellcat")


@pytest.fixture(autouse=True)
def _isolate_cache():
    """Snapshot + restore the module-level cache so tests don't
    leak override values into each other (or into other suites)."""
    snapshot = {lane: dict(v) for lane, v in _CACHE.items()}
    _CACHE.clear()
    try:
        yield
    finally:
        _CACHE.clear()
        _CACHE.update(snapshot)


# ─────────────── 1) empty cache → doctrine default ────────────────

@pytest.mark.parametrize("brain_id", BRAINS)
def test_effective_min_confidence_falls_back_to_doctrine_when_cache_empty(brain_id):
    """The helper MUST be behaviorally identical to reading
    `doctrine.min_confidence` directly when the operator has set
    no override. This guarantees adding the helper layer doesn't
    drift any brain's default behavior."""
    doctrine = DOCTRINES[brain_id]
    assert _CACHE == {}, "fixture broken: cache should be empty"
    assert effective_min_confidence(doctrine, "equity") == doctrine.min_confidence


@pytest.mark.parametrize("brain_id", BRAINS)
def test_effective_min_gap_falls_back_to_doctrine_when_cache_empty(brain_id):
    doctrine = DOCTRINES[brain_id]
    assert effective_min_gap(doctrine, "equity") == doctrine.min_gap


# ───────────── 2) populated cache → operator override wins ───────

@pytest.mark.parametrize("brain_id", BRAINS)
def test_operator_override_reaches_min_confidence(brain_id):
    """THE money test — locks the bug-fix doctrine. If a future
    refactor accidentally removes the `get_override` call from
    `effective_min_confidence`, this assertion fails immediately."""
    doctrine = DOCTRINES[brain_id]
    _CACHE["equity"] = {"min_confidence": 0.25}
    eff = effective_min_confidence(doctrine, "equity")
    assert eff == 0.25, (
        f"{brain_id}: operator UI override (0.25) was ignored — "
        f"got {eff}, doctrine default is {doctrine.min_confidence}. "
        "The UI tuning knobs are placebo again — see "
        "shared/brains/_doctrine_overrides.py and the strategy "
        "files for the read path."
    )


@pytest.mark.parametrize("brain_id", BRAINS)
def test_operator_override_reaches_min_gap(brain_id):
    doctrine = DOCTRINES[brain_id]
    _CACHE["equity"] = {"min_gap": 0.02}
    assert effective_min_gap(doctrine, "equity") == 0.02


def test_override_lane_isolation():
    """An override on `equity` must NOT leak into `crypto` and
    vice-versa. The operator should be able to tune the two lanes
    independently."""
    doctrine = DOCTRINES["barracuda"]
    _CACHE["equity"] = {"min_confidence": 0.20}
    _CACHE["crypto"] = {"min_confidence": 0.70}
    assert effective_min_confidence(doctrine, "equity") == 0.20
    assert effective_min_confidence(doctrine, "crypto") == 0.70


def test_partial_override_only_overrides_specified_key():
    """If the operator only sets `min_confidence`, leaving
    `min_gap` untouched, the gap MUST still resolve to the
    doctrine default — partial UI flips can't have unintended
    spillover."""
    doctrine = DOCTRINES["camino"]
    _CACHE["equity"] = {"min_confidence": 0.30}  # gap NOT set
    assert effective_min_confidence(doctrine, "equity") == 0.30
    assert effective_min_gap(doctrine, "equity") == doctrine.min_gap


def test_unknown_lane_falls_back_to_doctrine():
    """An override exists for `equity` but the caller asks for
    `unknown` (e.g. a future lane). MUST fall back to doctrine
    default rather than returning the equity value."""
    doctrine = DOCTRINES["gto"]
    _CACHE["equity"] = {"min_confidence": 0.20}
    assert (
        effective_min_confidence(doctrine, "futures")  # lane not in cache
        == doctrine.min_confidence
    )


# ─────────── 3) import-side regression — strategies wire helper ─

@pytest.mark.parametrize("brain_id", BRAINS)
def test_strategy_module_imports_effective_min_confidence(brain_id):
    """Each strategy MUST import `effective_min_confidence` at module
    scope. If someone removes this import during a refactor, the
    operator UI overrides go back to being placebo — this test fails
    before the silent regression reaches production."""
    import importlib
    mod = importlib.import_module(f"shared.brains.{brain_id}.strategy")
    src = mod.__file__
    assert src is not None
    with open(src, encoding="utf-8") as f:
        body = f.read()
    assert "from shared.brains._doctrine_overrides import effective_min_confidence" in body, (
        f"{brain_id}/strategy.py is no longer importing "
        f"effective_min_confidence — the operator UI overrides are "
        f"silently placebo again. Restore the import + the "
        f"`min_conf = effective_min_confidence(doctrine, ...)` line."
    )


@pytest.mark.parametrize("brain_id", BRAINS)
def test_strategy_no_longer_reads_doctrine_min_confidence_directly(brain_id):
    """No strategy should reference `doctrine.min_confidence` in
    its `if confidence < ...` guard — the override-aware
    `min_conf` local must be used instead. If this test fails,
    one of the BUY/SHORT branches has been reverted to the
    pre-fix pattern."""
    import importlib
    mod = importlib.import_module(f"shared.brains.{brain_id}.strategy")
    src = mod.__file__
    assert src is not None
    with open(src, encoding="utf-8") as f:
        body = f.read()
    # `doctrine` may still appear in docstrings / DOCTRINES[...] —
    # we only forbid the floor-check pattern.
    bad = "if confidence < doctrine.min_confidence"
    assert bad not in body, (
        f"{brain_id}/strategy.py contains `{bad}` — the operator "
        f"UI override is being bypassed by this guard. Replace "
        f"with `if confidence < min_conf`."
    )
