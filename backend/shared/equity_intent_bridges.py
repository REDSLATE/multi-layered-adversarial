"""Equity-lane intent bridges for all four brains.

One file (per operator directive: "they all should be both"). Mirrors
what the crypto bridges do for GTO + Hellcat — but generated through
`intent_bridge_factory.make_intent_bridge` so we don't duplicate the
~250 lines of bridge plumbing per brain.

Each entry maps a canonical brain identifier to its runtime alias:
    camino    → alpha     (intent_id prefix: alpha-equity-...)
    barracuda → camaro    (camaro-equity-...)
    hellcat   → chevelle  (chevelle-equity-...)
    gto       → redeye    (redeye-equity-...)

Routes (all admin-protected via `get_current_user`):
    POST /api/admin/{brain}/equity-bridge/emit
    GET  /api/admin/{brain}/equity-bridge/authority

Doctrine remains LANE-SCOPED, not pair-scoped — `requires_final_authority`
is stamped from `seats_with_execute("equity")` so it routes to whoever
currently holds the equity execute seat, regardless of which brain
authored the analysis. Same model the crypto bridges already use.
"""
from __future__ import annotations

from shared.intent_bridge_factory import BridgeConfig, make_intent_bridge


# (canonical_brain_id, runtime_alias)
_BRAIN_ALIASES = [
    ("camino",    "alpha"),
    ("barracuda", "camaro"),
    ("hellcat",   "chevelle"),
    ("gto",       "redeye"),
]


# Build a (build_fn, emit_fn, router) triple per brain. We expose them
# as a dict keyed by canonical brain id so test code and the router
# registry can import what they need without juggling globals.
EQUITY_BRIDGES: dict[str, dict] = {}
EQUITY_ROUTERS: list = []

for _brain_id, _alias in _BRAIN_ALIASES:
    _build, _emit, _router = make_intent_bridge(BridgeConfig(
        brain_id=_brain_id,
        lane="equity",
        runtime_alias=_alias,
        roadguard_name="EquityRoadGuard",
        route_prefix=f"/admin/{_brain_id}/equity-bridge",
    ))
    EQUITY_BRIDGES[_brain_id] = {
        "build": _build,
        "emit":  _emit,
        "router": _router,
    }
    EQUITY_ROUTERS.append(_router)


# Convenience accessors — match the per-brain naming pattern callers
# may expect. (Tests in particular import `build_camino_equity_intent`
# etc. for readability.)
build_camino_equity_intent     = EQUITY_BRIDGES["camino"]["build"]
emit_camino_equity_intent      = EQUITY_BRIDGES["camino"]["emit"]
build_barracuda_equity_intent  = EQUITY_BRIDGES["barracuda"]["build"]
emit_barracuda_equity_intent   = EQUITY_BRIDGES["barracuda"]["emit"]
build_hellcat_equity_intent    = EQUITY_BRIDGES["hellcat"]["build"]
emit_hellcat_equity_intent     = EQUITY_BRIDGES["hellcat"]["emit"]
build_gto_equity_intent        = EQUITY_BRIDGES["gto"]["build"]
emit_gto_equity_intent         = EQUITY_BRIDGES["gto"]["emit"]
