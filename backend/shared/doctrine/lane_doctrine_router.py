"""Lane-aware doctrine router.

Single entry point for the intent ingest path. Inspects `snapshot["lane"]`
and routes to the correct twin doctrine. Unknown / missing lane gets a
hard REJECT packet so the operator can see "no doctrine" was applied
without the absence being silent.

Doctrine pins (2026-02-17):
    * TWO LANES, no third. `equity` and `crypto`. Anything else → REJECT.
    * Twin doctrine — equity sidecar in `shared.doctrine.brain_sidecars`,
      crypto sidecar in `shared.crypto.doctrine.crypto_brain_sidecars`.
      Neither imports the other. Lazy imports preserve that. Regression
      test: `tests/test_lane_isolation.py`.
    * Restrictions are on the SEAT, not the brain — both packets use a
      role-keyed `seats: {strategist, adversary, governor,
      execution_judge}` shape with a `holder` field per seat.
    * Brains can occupy MULTIPLE SEATS across lanes (e.g. Alpha in
      equity-decider AND crypto-decider), so `seat_holders` is a flat
      dict that the caller fills from the live roster.
"""
from __future__ import annotations

from typing import Any, Dict, Optional


def build_lane_doctrine_packet(
    snapshot: Dict[str, Any],
    seat_holders: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    lane = str(snapshot.get("lane") or "").lower()

    if lane == "equity":
        from shared.doctrine.brain_sidecars import (  # noqa: WPS433
            build_all_brain_doctrine_packets,
        )
        return build_all_brain_doctrine_packets(snapshot, seat_holders)

    if lane == "crypto":
        from shared.crypto.doctrine.crypto_brain_sidecars import (  # noqa: WPS433
            build_crypto_brain_doctrine_packet,
        )
        return build_crypto_brain_doctrine_packet(snapshot, seat_holders)

    return {
        "event_type": "BRAIN_DOCTRINE_SIDECAR_PACKET",
        "doctrine_version": "unknown_lane_reject_v1",
        "lane": lane or "UNKNOWN",
        "symbol": snapshot.get("symbol", "UNKNOWN"),
        "base_labels": {
            "score": 0.0,
            "quality": "REJECT",
            "labels": ["UNKNOWN_LANE"],
            "reasons": ["doctrine router received unknown lane"],
        },
        "seats": {},
    }


def hoist_packet_audit_fields(packet: Dict[str, Any]) -> Dict[str, Any]:
    """Flatten the audit-relevant fields out of a role-keyed packet.

    Both lanes now share the same role-keyed shape, so this is a
    simple read. We still defensively handle the legacy equity shape
    (top-level `alpha`/`redeye`/`chevelle`/`camaro` keys) just in case
    an older audit row gets re-hoisted by some caller.
    """
    if not packet:
        return {
            "quality": None, "score": None,
            "redeye_challenge_required": None,
            "chevelle_governor_action": None,
            "camaro_execution_ready": None,
        }

    # New role-keyed shape (equity + crypto both use this now)
    if "seats" in packet:
        base = packet.get("base_labels") or {}
        seats = packet.get("seats") or {}
        adversary = seats.get("adversary") or {}
        governor = seats.get("governor") or {}
        execution_judge = seats.get("execution_judge") or {}
        challenge_required = bool(adversary.get("challenge_required"))
        if not challenge_required:
            # Crypto adversary uses challenge_strength + objections
            if adversary.get("objections"):
                challenge_required = True
            cs = adversary.get("challenge_strength")
            if isinstance(cs, (int, float)) and cs > 0.0 and adversary.get("objections"):
                challenge_required = True
        return {
            "quality": base.get("quality"),
            "score": base.get("score"),
            "redeye_challenge_required": challenge_required,
            "chevelle_governor_action": governor.get("governor_action"),
            "camaro_execution_ready": bool(execution_judge.get("execution_ready")),
        }

    # Legacy equity shape — only here for back-compat with rows already
    # persisted before the role-keyed refactor.
    alpha = packet.get("alpha") or {}
    doctrine = alpha.get("doctrine") or {}
    redeye = packet.get("redeye") or {}
    chevelle = packet.get("chevelle") or {}
    camaro = packet.get("camaro") or {}
    return {
        "quality": doctrine.get("quality"),
        "score": doctrine.get("score"),
        "redeye_challenge_required": bool(redeye.get("challenge_required")),
        "chevelle_governor_action": chevelle.get("governor_action"),
        "camaro_execution_ready": bool(camaro.get("execution_ready")),
    }


# ─── helper to assemble seat_holders from the live roster ───────────

async def fetch_seat_holders(lane: str) -> Dict[str, Optional[str]]:
    """Read the current roster and return `{seat_name: brain_or_None}`
    for the four doctrine-relevant seats in `lane`.

    For equity: decider, opponent, governor, executor.
    For crypto: crypto_decider, crypto_opponent, crypto_governor, crypto.

    Returns an empty dict for any other lane (the router will produce
    an UNKNOWN_LANE_REJECT packet, no holders needed).

    Doctrine: brains can hold multiple seats across lanes, so this
    intentionally does NOT deduplicate. If alpha holds both
    equity-decider and crypto-decider, both packets show alpha as the
    strategist seat holder.
    """
    lane_norm = (lane or "").lower()
    if lane_norm not in ("equity", "crypto"):
        return {}
    from shared.roster import get_roster  # noqa: WPS433
    r = await get_roster()
    assignments = (r or {}).get("assignments") or {}
    if lane_norm == "equity":
        keys = ("decider", "opponent", "governor", "executor")
    else:
        keys = ("crypto_decider", "crypto_opponent", "crypto_governor", "crypto")
    return {k: assignments.get(k) for k in keys}
