"""Operator-override-aware doctrine threshold resolver.

2026-02-25 — diagnostic discovery: `shared/brain_tuning_cache.py`
defines `get_override(lane, key)` and runs a 30s Mongo refresher
loop populating the cache from `runtime_flags._id="brain_tuning"`.
The cache works. Nothing reads from it. All 4 brain strategies
(barracuda, camino, gto, hellcat) bypass the override path entirely
and read `doctrine.min_confidence` directly from the compiled-in
`brain_doctrine.py` defaults.

Net effect: the operator's UI tuning knobs ("less conservative"
slider on the Brain Tuning page) were *placebo* — the value
travelled UI → POST → Mongo → cache → and died there. Brains kept
firing against the hardcoded doctrine floor.

This module is the missing read path. Each strategy resolves its
effective floor through here once per `evaluate()` call:

    from shared.brains._doctrine_overrides import effective_min_confidence
    doctrine = DOCTRINES["barracuda"]
    min_conf = effective_min_confidence(doctrine, lane="equity")
    if confidence < min_conf:
        return _hold(f"...{min_conf}...", ...)

`lane` defaults to `"equity"` because the native brain runners
(`_runner_core.run_tick_for_brain`) operate on the equity universe
today. When crypto bridge code starts calling `evaluate()` (it
doesn't today — crypto goes through separate bridge modules),
the caller passes `lane="crypto"` explicitly.

Behaviorally identical to doctrine defaults when no override is
set — pure additive. No regressions possible from this read path.
"""
from __future__ import annotations

from shared.brain_doctrine import BrainDoctrine
from shared.brain_tuning_cache import get_override


def effective_min_confidence(
    doctrine: BrainDoctrine, lane: str = "equity",
) -> float:
    """Return the operator's UI-override floor for `min_confidence`
    if set, otherwise the brain's compiled doctrine default.

    Never raises. `None` / missing-cache cases fall back cleanly.
    """
    override = get_override(lane, "min_confidence")
    if override is None:
        return float(doctrine.min_confidence)
    return float(override)


def effective_min_gap(
    doctrine: BrainDoctrine, lane: str = "equity",
) -> float:
    """Return the operator's UI-override floor for `min_gap` if set,
    otherwise the doctrine default.

    Currently unused by the brain strategies (which gate on
    `min_confidence` only). Exposed here so the next strategy
    update that wires confidence-gap math has the resolver
    ready — eliminates a future round-trip through this same
    discovery process.
    """
    override = get_override(lane, "min_gap")
    if override is None:
        return float(doctrine.min_gap)
    return float(override)


__all__ = ["effective_min_confidence", "effective_min_gap"]
