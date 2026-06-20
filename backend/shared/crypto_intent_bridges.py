"""Crypto-lane intent bridges for camino + barracuda.

GTO and Hellcat already have legacy crypto bridges
(`shared/redeye_crypto_intent_bridge.py`,
`shared/chevelle_crypto_intent_bridge.py`). This module adds the
missing two so EVERY brain × EVERY lane has an admin emit surface:

    Brain      | Crypto bridge                       | Equity bridge
    -----------|-------------------------------------|----------------------------
    camino     | /api/admin/camino/crypto-bridge     | /api/admin/camino/equity-bridge
    barracuda  | /api/admin/barracuda/crypto-bridge  | /api/admin/barracuda/equity-bridge
    hellcat    | /api/admin/hellcat/bridge (legacy)  | /api/admin/hellcat/equity-bridge
    gto        | /api/admin/redeye/bridge  (legacy)  | /api/admin/gto/equity-bridge

Generated via `shared.intent_bridge_factory.make_intent_bridge`. Same
doctrine guards as every other bridge — lane-scoped seat authority,
research evidence on the side, never an execution call.

Route prefix choice (`{brain}/crypto-bridge` instead of mirroring the
legacy GTO/Hellcat `{alias}/bridge` shape) is intentional: the new
URLs are lane-explicit so the operator can predict the URL across
all four brains without remembering which use the legacy form.
"""
from __future__ import annotations

from shared.intent_bridge_factory import BridgeConfig, make_intent_bridge


# Only the two brains that don't already have a crypto bridge. GTO
# (redeye) and Hellcat (chevelle) stay on their legacy routes; adding
# them here would either collide on routes (if same prefix) or
# duplicate the surface (if different prefix) for no gain.
_BRAIN_ALIASES = [
    ("camino",    "alpha"),
    ("barracuda", "camaro"),
]


CRYPTO_BRIDGES: dict[str, dict] = {}
CRYPTO_ROUTERS: list = []

for _brain_id, _alias in _BRAIN_ALIASES:
    _build, _emit, _router = make_intent_bridge(BridgeConfig(
        brain_id=_brain_id,
        lane="crypto",
        runtime_alias=_alias,
        roadguard_name="CryptoRoadGuard",
        route_prefix=f"/admin/{_brain_id}/crypto-bridge",
    ))
    CRYPTO_BRIDGES[_brain_id] = {
        "build":  _build,
        "emit":   _emit,
        "router": _router,
    }
    CRYPTO_ROUTERS.append(_router)


# Convenience accessors for tests / direct callers.
build_camino_crypto_intent     = CRYPTO_BRIDGES["camino"]["build"]
emit_camino_crypto_intent      = CRYPTO_BRIDGES["camino"]["emit"]
build_barracuda_crypto_intent  = CRYPTO_BRIDGES["barracuda"]["build"]
emit_barracuda_crypto_intent   = CRYPTO_BRIDGES["barracuda"]["emit"]
