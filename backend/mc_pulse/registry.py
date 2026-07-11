"""Brain registry.

Owns the single source of truth for "which brains exist" and
"which brain trades which lane." No mutable side channels — a
brain is either in the registry at startup or it isn't.

Design freeze: `MC_PULSE.md` §1, §3. `mc_brains/` will register
each brain in `lifespan.py` at server startup (see next migration
step).
"""
from __future__ import annotations

import logging
from typing import Iterable

from mc_pulse.protocols import Brain

logger = logging.getLogger("mc_pulse.registry")


class BrainRegistry:
    """Simple ordered container. Not a singleton, but the app-wide
    instance is set once in `lifespan.on_startup` — see
    `mc_pulse.get_registry`."""

    def __init__(self):
        self._brains: dict[str, Brain] = {}

    def register(self, brain: Brain) -> None:
        if brain.id in self._brains:
            raise ValueError(f"brain already registered: {brain.id!r}")
        # Runtime protocol check catches "forgot to implement
        # `evaluate` async" mistakes at boot, not at first pulse.
        if not isinstance(brain, Brain):
            raise TypeError(
                f"{brain!r} does not satisfy the Brain protocol "
                "(missing id / lanes / evaluate?)"
            )
        self._brains[brain.id] = brain
        logger.info(
            "registered brain id=%s lanes=%s cadence=%ss timeout=%ss",
            brain.id, sorted(brain.lanes),
            brain.cadence_seconds, brain.evaluation_timeout_seconds,
        )

    def all(self) -> Iterable[Brain]:
        return tuple(self._brains.values())

    def for_lane(self, lane: str) -> Iterable[Brain]:
        return tuple(b for b in self._brains.values() if lane in b.lanes)

    def get(self, brain_id: str) -> Brain:
        return self._brains[brain_id]

    def ids(self) -> list[str]:
        return sorted(self._brains.keys())

    def __len__(self) -> int:
        return len(self._brains)


# Module-level singleton, populated at server startup by
# `lifespan.on_startup`. Kept module-level (not a class attribute)
# so tests can swap it via `set_registry`.
_registry: BrainRegistry = BrainRegistry()


def get_registry() -> BrainRegistry:
    return _registry


def set_registry(new_registry: BrainRegistry) -> BrainRegistry:
    """Replace the module-level registry. Used only in tests —
    the production path calls `get_registry().register()` from
    lifespan on startup."""
    global _registry
    old = _registry
    _registry = new_registry
    return old
