"""Shared pattern detectors namespace.

Each pattern module is a pure-function detector that takes a window
of OHLCV bars and returns a typed signals packet. Stored on the
shared technical feed and `shared_pattern_snapshots` so brains can
read the same evidence. Brains decide how to weight; MC never gates.
"""
from shared.patterns.base_breakout import (
    PatternSignals,
    detect_pattern,
    reload_env,
)

__all__ = ["PatternSignals", "detect_pattern", "reload_env"]
