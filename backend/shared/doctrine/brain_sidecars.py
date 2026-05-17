"""Brain doctrine sidecar packet — one snapshot, four interpretations.

Doctrine (2026-02-17):
    A single market snapshot of a symbol gets run through all four
    brain interpreters in parallel. The resulting packet is what MC
    attaches to a mission / trade candidate so every downstream
    consumer (Shelly memory, council, audit) sees the same shared
    `DoctrineLabels` plus each brain's role-flavored take.

    None of these interpreters execute. None of them create direction.
    They produce ADVISORY signals only:
        Alpha     → conviction_delta
        REDEYE    → objections + challenge_strength
        Chevelle  → risk_multiplier + block_reasons
        Camaro    → execution_ready + checks

    The doctrine is small-account-equity flavored (gap_pct,
    relative_volume, float_millions, $1-20 price range, etc.). Crypto
    callers should NOT use this packet — the crypto lane has its own
    doctrine pending.
"""
from __future__ import annotations

from typing import Any, Dict

from runtimes.alpha.doctrine_interpreter import interpret_for_alpha
from runtimes.camaro.doctrine_interpreter import interpret_for_camaro
from runtimes.chevelle.doctrine_interpreter import interpret_for_chevelle
from runtimes.redeye.doctrine_interpreter import interpret_for_redeye


def build_all_brain_doctrine_packets(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """One snapshot, four different interpretations.

    MC can attach this packet to a mission / trade candidate.
    Shelly can store it for later verified learning.
    """
    return {
        "event_type": "BRAIN_DOCTRINE_SIDECAR_PACKET",
        "symbol": snapshot.get("symbol", "UNKNOWN"),
        "alpha": interpret_for_alpha(snapshot),
        "redeye": interpret_for_redeye(snapshot),
        "chevelle": interpret_for_chevelle(snapshot),
        "camaro": interpret_for_camaro(snapshot),
        "doctrine_version": "small_account_sidecar_v1",
    }
