"""Seat policy — the single source of authority truth.

Doctrine:
    Identity does not grant authority. Seat policy does.

A brain has zero permissions by virtue of being itself; permissions
flow from the seat the brain currently occupies. When a brain moves
out of a seat, those permissions evaporate the same instant.

Every opinion / stance / decision posted by a brain carries a snapshot
of its current seat's policy at write time (`may_execute`,
`may_override`, `may_decide`, `may_veto`, `seat_epoch`). This lets us
audit historical actions against the rules that were in effect when
they happened — even if the brain's seat changed later.

Phase 1: All `may_execute` bits remain operationally inert (no orders
fire from any code path). This module is the *contract* the broker
exec-gate will consult in Phase 2.
"""
from __future__ import annotations

from typing import TypedDict


class SeatPolicy(TypedDict):
    """Declarative permissions for one seat."""
    may_decide: bool       # may form the trust / reduce / veto call on a position
    may_execute: bool      # may route orders (gated again at the broker layer)
    may_override: bool     # may overrule a peer's stance (decider primarily)
    may_veto: bool         # may halt promotion / freeze a runtime (governor)
    speaks_as: str         # human-readable label printed on stances


SEAT_POLICY: dict[str, SeatPolicy] = {
    "decider": {
        "may_decide": True,
        "may_execute": False,
        "may_override": True,
        "may_veto": False,
        "speaks_as": "decider",
    },
    "executor": {
        "may_decide": True,
        # Phase 1: schema-pinned False at every endpoint. The flag exists
        # so Phase 2's broker exec-gate can consult it; flipping a brain
        # into the executor seat does NOT in itself enable trading.
        "may_execute": True,
        "may_override": False,
        "may_veto": False,
        "speaks_as": "executor",
    },
    "governor": {
        "may_decide": False,
        "may_execute": False,
        "may_override": False,
        "may_veto": True,
        "speaks_as": "governor",
    },
    "advisor": {
        "may_decide": False,
        "may_execute": False,
        "may_override": False,
        "may_veto": False,
        "speaks_as": "advisor",
    },
    "opponent": {
        # The adversarial seat — argues the contrary case. Distinguished
        # from advisor only by training intent; both are non-deciding.
        "may_decide": False,
        "may_execute": False,
        "may_override": False,
        "may_veto": False,
        "speaks_as": "opponent",
    },
}

# All recognized seat names.
SEATS: tuple[str, ...] = tuple(SEAT_POLICY.keys())


def snapshot(seat: str | None) -> dict:
    """Snapshot the policy for the given seat. Returns an empty-permission
    record when the brain holds no seat — that's the safest default.

    The seat string is normalized to lowercase. Unknown seats also fall
    through to the empty-permission record (NOT raise) because the
    operator may have invented a seat name in eligibility settings that
    isn't in the policy yet — we'd rather log + ingest the stance with
    `may_*=False` than reject it.
    """
    if seat is None:
        return {
            "posted_as": None,
            "may_decide": False,
            "may_execute": False,
            "may_override": False,
            "may_veto": False,
        }
    s = seat.lower()
    p = SEAT_POLICY.get(s)
    if not p:
        return {
            "posted_as": s,
            "may_decide": False,
            "may_execute": False,
            "may_override": False,
            "may_veto": False,
        }
    return {
        "posted_as": s,
        "may_decide": p["may_decide"],
        "may_execute": p["may_execute"],
        "may_override": p["may_override"],
        "may_veto": p["may_veto"],
    }
