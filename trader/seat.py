"""Seat reader — honors operator's seat doctrine in MC's Mongo.

The trader does NOT manage seats. The operator manages them through
MC's existing seat UI (or directly in `seat_registry` /
`shared_brain_roster` collections). The trader reads them, applies
them, and never writes.

Resolution order (highest wins):
    1. `seat_registry` row for `(lane, role)` if `holder` is set
    2. `shared_brain_roster.assignments` legacy fallback
    3. Hard-coded default (see DEFAULT_SEATS) — only used when
       neither source is populated

DEFAULT_SEATS preserves the angel-doctrine starting positions so the
trader never sits idle on a fresh deploy. Operator can override at
any time via MC.
"""
from __future__ import annotations

import logging
from typing import Optional

from trader import config


logger = logging.getLogger("trader.seat")


# Hard-coded fallback assignments. The angel-named seats are
# constants of the architecture — Raziel, Nuriel, Paschar, Sariel
# on equity; Remiel, Cassiel, Israfel, Zadkiel on crypto. The
# brain-in-seat is the rotation. These defaults pair compatible
# doctrines at the top two seats (strategist + executor) so the
# strict-agreement rule (operator pin 2026-06-30) produces trades
# instead of a permanent HOLD machine.
#
#   Equity:
#     Raziel  (strategist) = Camino   (trend continuation)
#     Nuriel  (governor)   = Hellcat  (breakout) — risk modifier
#     Paschar (executor)   = GTO      (momentum) — authorizes
#     Sariel  (auditor)    = Barracuda (mean rev) — observability
#
#   Crypto:
#     Remiel  (strategist) = Hellcat  (breakout)
#     Cassiel (governor)   = Camino   (trend) — risk modifier
#     Israfel (executor)   = GTO      (momentum) — authorizes
#     Zadkiel (auditor)    = Barracuda (mean rev) — observability
#
# Operator can override at any time via `seat_registry` in Mongo.
DEFAULT_SEATS = {
    "equity": {
        "strategist": "camino",     # Raziel
        "governor":   "hellcat",    # Nuriel
        "executor":   "gto",        # Paschar
        "auditor":    "barracuda",  # Sariel
    },
    "crypto": {
        "strategist": "hellcat",    # Remiel
        "governor":   "camino",     # Cassiel
        "executor":   "gto",        # Israfel
        "auditor":    "barracuda",  # Zadkiel
    },
}


async def get_lane_seats(db, lane: str) -> dict[str, Optional[str]]:
    """Return all 4 role-holders for the lane in one snapshot.
    Reads `seat_registry` first; falls back to legacy roster, then
    to DEFAULT_SEATS so the trader is never seat-vacant."""
    out: dict[str, Optional[str]] = {}
    lane_l = (lane or "").lower()
    defaults = DEFAULT_SEATS.get(lane_l, {})

    for role in config.ROLES:
        sid = f"{lane_l}:{role}"
        doc = await db["seat_registry"].find_one(
            {"_id": sid}, {"_id": 0, "holder": 1}
        )
        if doc and doc.get("holder"):
            out[role] = doc["holder"]
            continue
        # Legacy roster fallback.
        legacy_keys = [role] if lane_l == "equity" else [f"{lane_l}_{role}"]
        roster = await db["shared_brain_roster"].find_one(
            {}, {"_id": 0, "assignments": 1}
        )
        assignments = (roster or {}).get("assignments") or {}
        legacy_holder = None
        for k in legacy_keys:
            if assignments.get(k):
                legacy_holder = assignments[k]
                break
        out[role] = legacy_holder or defaults.get(role)
    return out


async def governor_multiplier(db, lane: str) -> float:
    """Read the governor's risk multiplier for the lane (bounded
    [0.0, 2.0]). Default 1.0."""
    sid = f"{(lane or '').lower()}:governor"
    doc = await db["seat_registry"].find_one(
        {"_id": sid}, {"_id": 0, "risk_multiplier": 1}
    )
    raw = (doc or {}).get("risk_multiplier")
    if raw is None:
        return 1.0
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return 1.0
    return max(0.0, min(2.0, v))
