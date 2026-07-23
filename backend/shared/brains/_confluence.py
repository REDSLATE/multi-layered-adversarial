"""Shared 2-of-3 confluence scoring for native brain doctrines.

2026-06 doctrine relaxation (operator-approved): strict AND chains
caused Camino/GTO/Hellcat to forfeit ~95-99% of ticks to Barracuda.
Full confluence keeps legacy behavior; exactly one missing gate emits
a dampened half-size PROBE so the brain re-earns trust through the
outcome learning loop instead of staying silent.
"""
from __future__ import annotations

from typing import Sequence

PARTIAL_PENALTY = 0.85
PARTIAL_SIZE_MULT = 0.5


def confluence_signal(
    raw_signal: float, gates: Sequence[bool],
) -> tuple[float, str, int]:
    """(signal, mode, gates_passed). mode is full|partial|none."""
    passed = sum(1 for g in gates if g)
    if passed >= len(gates):
        return raw_signal, "full", passed
    if passed == len(gates) - 1:
        return raw_signal * PARTIAL_PENALTY, "partial", passed
    return 0.0, "none", passed


__all__ = ["confluence_signal", "PARTIAL_PENALTY", "PARTIAL_SIZE_MULT"]
