"""Doctrine Registry — universe class → doctrine packet builder.

Doctrine pin (2026-02-19, operator directive):
    Registry is the single dispatch point for doctrine packet
    construction. Callers hand it a classified snapshot; it returns
    the packet from the registered builder. Adding a new universe
    class is a one-line `register()` call — no more branching in the
    lane router.

    Builders share the same signature so the registry can call any
    of them uniformly:

        (snapshot, seat_holders) -> packet_dict

    where `packet_dict` matches the shape emitted by
    `large_cap_doctrine.build_large_cap_doctrine_packet` (role-keyed
    seats, `base_labels`, `doctrine_version`).

    Lazy imports keep the registry lean — pulling the crypto
    doctrine module for an equity intent would double-tag the audit
    log and confuse operators.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from shared.doctrine.universe_classifier import UniverseClass, classify_universe

# Builder signature: (snapshot, seat_holders) -> packet
DoctrineBuilder = Callable[[Dict[str, Any], Optional[Dict[str, str]]], Dict[str, Any]]


_REGISTRY: Dict[UniverseClass, DoctrineBuilder] = {}


def register(universe_class: UniverseClass, builder: DoctrineBuilder) -> None:
    """Register a doctrine builder for a universe class."""
    _REGISTRY[universe_class] = builder


def is_registered(universe_class: UniverseClass) -> bool:
    return universe_class in _REGISTRY


def _build_unknown_packet(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """Build the classifier-gap short-circuit packet.

    Doctrine pin (2026-02-19): This is NOT a REJECT — REJECT means
    "scored and failed", which implies the doctrine WAS applied.
    An unclassified snapshot means NO doctrine was applied at all,
    same shape as the enricher's `enrichment_status == "failed"`
    NO_DATA short-circuit. The operator's UI must render "no
    doctrine" honestly instead of a per-symbol verdict.
    """
    return {
        "event_type": "BRAIN_DOCTRINE_SIDECAR_PACKET",
        "doctrine_version": "unknown_universe_no_data_v1",
        "lane": str(snapshot.get("lane") or "UNKNOWN"),
        "symbol": snapshot.get("symbol", "UNKNOWN"),
        "base_labels": {
            "score": 0.0,
            "quality": "NO_DATA",
            "labels": ["UNKNOWN_UNIVERSE"],
            "reasons": [
                "universe classifier could not resolve this snapshot; "
                "no pinned roster hit, no market_cap_band, no strategy "
                "hint — routing gap, not a scored rejection",
            ],
        },
        "seats": {},
    }


def dispatch(
    snapshot: Dict[str, Any],
    seat_holders: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Classify `snapshot`, dispatch to the registered builder, and
    stamp the resolved universe class on the packet metadata.

    Returns a REJECT packet if no builder is registered for the
    classified universe.
    """
    uc = classify_universe(snapshot)
    builder = _REGISTRY.get(uc)
    if builder is None:
        packet = _build_unknown_packet(snapshot)
        packet["universe_class"] = uc.value
        return packet
    packet = builder(snapshot, seat_holders)
    # Non-invasive metadata tag so audit rows carry the classifier's
    # verdict alongside the doctrine_version.
    packet["universe_class"] = uc.value
    return packet


def _wire_default_registry() -> None:
    """Wire the default builders. Called on module import. Lazy inner
    imports so the registry file itself has no doctrine dependencies
    at import time."""

    def _large_cap_builder(snap, holders):
        from shared.doctrine.large_cap_doctrine import (
            build_large_cap_doctrine_packet,
        )
        return build_large_cap_doctrine_packet(snap, holders)

    def _small_cap_builder(snap, holders):
        # Small-cap momentum has TWO strategy-specific variants
        # (gap_and_go / micro_pullback) plus a generic sidecar
        # fallback. The strategy-specific builders are preferred
        # when the caller supplied a `strategy` hint.
        strategy = str(snap.get("strategy") or "").lower()
        if strategy in ("gap_and_go", "micro_pullback"):
            from shared.doctrine.strategy_doctrines import (
                build_strategy_packet,
            )
            packet = build_strategy_packet(strategy, snap, holders)
            if packet is not None:
                return packet
        from shared.doctrine.brain_sidecars import (
            build_all_brain_doctrine_packets,
        )
        return build_all_brain_doctrine_packets(snap, holders)

    def _etf_builder(snap, holders):
        # ETFs share the large-cap doctrine (liquidity + regime
        # scoring maps cleanly). The packet gets an `ETF` universe
        # stamp so Patent J can graduate ETF slices independently
        # of single-name mega-caps.
        from shared.doctrine.large_cap_doctrine import (
            build_large_cap_doctrine_packet,
        )
        return build_large_cap_doctrine_packet(snap, holders)

    def _crypto_builder(snap, holders):
        from shared.crypto.doctrine.crypto_brain_sidecars import (
            build_crypto_brain_doctrine_packet,
        )
        return build_crypto_brain_doctrine_packet(snap, holders)

    register(UniverseClass.LARGE_CAP, _large_cap_builder)
    register(UniverseClass.SMALL_CAP_MOMENTUM, _small_cap_builder)
    register(UniverseClass.ETF, _etf_builder)
    register(UniverseClass.CRYPTO, _crypto_builder)


_wire_default_registry()
