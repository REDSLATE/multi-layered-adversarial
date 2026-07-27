"""Pipeline flow counters — emission health vs doctrine health.

Two independently verifiable flows (operator directive 2026-07-27):
  Brain emission:  MC pulse → brain evaluated → opinion → intent
  Doctrine:        intent received → snapshot enriched → graded
Cumulative in-process counters since backend start; cheap, lock-free
enough for the hot path (GIL-atomic dict increments).
"""
from __future__ import annotations

import time
from collections import defaultdict

_counts: dict[str, int] = defaultdict(int)
_started_at: float = time.time()

KNOWN = (
    "brains_evaluated", "brain_holds", "actionable_opinions",
    "intents_emitted", "intents_enriched", "intents_no_data",
    "intents_graded_no_data", "intents_rejected",
)


def incr(name: str, n: int = 1) -> None:
    _counts[name] += n


def snapshot() -> dict:
    out = {k: _counts.get(k, 0) for k in KNOWN}
    for k, v in _counts.items():
        out.setdefault(k, v)
    out["since"] = _started_at
    out["uptime_s"] = round(time.time() - _started_at, 1)
    return out


def reset_for_tests() -> None:
    global _started_at
    _counts.clear()
    _started_at = time.time()
