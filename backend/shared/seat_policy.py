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
    may_veto: bool         # may halt promotion / freeze a runtime (governor)
    seat_required: bool    # quorum: if True and seat unstamped, position is degraded
    speaks_as: str         # human-readable label printed on stances
    # Lane scope. `["equity"]`, `["crypto"]`, or `["equity","crypto"]` /
    # `None` for any. Execution rights apply ONLY to lanes in this list.
    # Doctrine: identity does not grant authority — neither does the lane.
    # If your seat doesn't list 'crypto', you cannot execute crypto, even
    # if you may_execute=True for equity. Same for the reverse.
    lane_scope: list[str] | None


# ─── Seat aliases (2026-02-19 deprecation; revised 2026-05-20) ──────
#
# Doctrine (after the 2026-05-20 PARADOX naming clarification):
#
#   AUDITOR is NOT a seat. It is an emergent function of the
#   (executor, opponent) interaction — the `paradox_record` artifact
#   the kernel produces on every gated intent. The earlier alias
#   `advisor → auditor` was structurally wrong; advisory work belongs
#   to the OPPONENT seat, which already speaks the contrary case.
#
# Final alias map:
#
#     decider  → executor   (executor already has may_decide=True;
#                            the separate decider seat was redundant)
#     advisor  → opponent   (advisory dissent is what the opponent
#                            seat is for; auditor isn't a seat at all)
#
# Crypto twins follow the same rule. The aliases let old sidecars
# that still send legacy seat names keep working — MC normalizes to
# the canonical name at every boundary.

SEAT_ALIASES: dict[str, str] = {
    "decider": "executor",
    "crypto_decider": "crypto",        # crypto executor slot is `crypto`
    "advisor": "opponent",
    "crypto_advisor": "crypto_opponent",
    # Old `auditor` references resolve to opponent — auditor is no
    # longer a seat under the PARADOX hierarchy.
    "auditor": "opponent",
    "crypto_auditor": "crypto_opponent",
}


def normalize_seat(seat: str | None) -> str | None:
    """Map a seat name through the alias table.

    Returns the canonical name when an alias is given; returns the
    input unchanged for canonical names and unknown values.
    Case-preserving on the lookup itself — case-insensitivity is
    handled by the lowercase normalization at every boundary that
    consumes a seat string.
    """
    if seat is None:
        return None
    return SEAT_ALIASES.get(seat, seat)


SEAT_POLICY: dict[str, SeatPolicy] = {
    "decider": {
        # DEPRECATED — kept in this table only so legacy reads against
        # the seat name resolve to a sensible policy. The alias machinery
        # in `snapshot()` rewrites incoming `decider` lookups to
        # `executor` before this row is even consulted. Do not add new
        # callers that read this row directly.
        "may_decide": True,
        "may_execute": False,
        "may_veto": False,
        "seat_required": False,
        "speaks_as": "decider",
        "lane_scope": None,
    },
    "executor": {
        "may_decide": True,
        # Phase 1: schema-pinned False at every endpoint. The flag exists
        # so Phase 2's broker exec-gate can consult it; flipping a brain
        # into the executor seat does NOT in itself enable trading.
        "may_execute": True,
        "may_veto": False,
        # Executor stance is needed to advance auto-mode positions —
        # required for quorum on every position regardless of call_mode.
        "seat_required": True,
        "speaks_as": "executor",
        # Equity-only by doctrine. Crypto routes through the dedicated
        # crypto seat so the two lanes are physically separated.
        "lane_scope": ["equity"],
    },
    "governor": {
        "may_decide": False,
        "may_execute": False,
        "may_veto": True,
        # Governor silence on a position = governance blindness.
        # Operator must SEE this loudly — that's how Chevelle going dark
        # gets caught before a bad call gets locked in.
        "seat_required": True,
        "speaks_as": "governor",
        "lane_scope": None,  # vetoes across lanes
    },
    "advisor": {
        # DEPRECATED — alias rewrites to `auditor`. See SEAT_ALIASES.
        "may_decide": False,
        "may_execute": False,
        "may_veto": False,
        "seat_required": False,
        "speaks_as": "advisor",
        "lane_scope": None,
    },
    "opponent": {
        # The adversarial seat — argues the contrary case. Distinguished
        # from advisor only by training intent; both are non-deciding.
        "may_decide": False,
        "may_execute": False,
        "may_veto": False,
        # Opponent silence = adversarial blindness, the exact failure
        # mode the operator flagged: "if REDEYE dies, you stop hearing
        # the contrary case and silently dial up risk." Required.
        "seat_required": True,
        "speaks_as": "opponent",
        "lane_scope": None,
    },
    "auditor": {
        # DEPRECATED (2026-05-20). Under the PARADOX hierarchy, auditor
        # is NOT a seat — it is the emergent paradox_record artifact
        # produced by the (executor, opponent) pair on every gated
        # intent. SEAT_ALIASES now maps `auditor → opponent` so any
        # legacy read transparently resolves to the contrary-case
        # seat. This row is retained ONLY for forensic queries
        # against historical receipts that recorded `seat=auditor`
        # before the alias change. New code MUST NOT reference it.
        "may_decide": False,
        "may_execute": False,
        "may_veto": False,
        "seat_required": False,
        "speaks_as": "auditor",
        "lane_scope": None,
    },
    "crypto": {
        # Dedicated crypto seat — observe, buy, sell crypto. No equity
        # rights. No deciding. No governing. No vetoing. Doctrine: a
        # crypto-specialized voice with execution rights scoped to the
        # crypto lane only. If MC's broker router gets a crypto intent
        # and this seat is empty, no crypto trade fires.
        "may_decide": False,
        "may_execute": True,
        "may_veto": False,
        # Crypto silence is its own loud flag — a frozen crypto seat
        # means MC is half-blind to the live crypto book.
        "seat_required": True,
        "speaks_as": "crypto",
        "lane_scope": ["crypto"],
    },
}

# All recognized seat names.
SEATS: tuple[str, ...] = tuple(SEAT_POLICY.keys())


def snapshot(seat: str | None) -> dict:
    """Snapshot the policy for the given seat. Returns an empty-permission
    record when the brain holds no seat — that's the safest default.

    The seat string is normalized to lowercase and run through the
    deprecation alias table (`SEAT_ALIASES`). Crypto-lane seat slots
    (`crypto`, `crypto_governor`, `crypto_auditor`, …) inherit the
    SAME role policy as their equity twin: `crypto_governor` → policy
    of `governor`, `crypto` → policy of `executor`, etc. This keeps a
    single source of truth for what each ROLE can do while the SLOT
    (which roster row) enforces lane isolation at the lookup layer
    (see `shared/council._seat_holder`).

    Aliases (2026-02-19): `decider`→`executor`, `advisor`→`auditor`,
    and their `crypto_*` twins. Old sidecars sending the deprecated
    seat names still resolve to a working policy without behavioral
    change because the alias targets carry the responsibilities the
    old names tried to express.

    Unknown seats fall through to the empty-permission record (NOT raise)
    because the operator may have invented a seat name in eligibility
    settings that isn't in the policy yet — we'd rather log + ingest
    the stance with `may_*=False` than reject it.
    """
    if seat is None:
        return {
            "posted_as": None,
            "may_decide": False,
            "may_execute": False,
            "may_veto": False,
        }
    s = seat.lower()
    # Apply alias normalization before any further resolution. Old
    # `decider` / `advisor` reads transparently become `executor` /
    # `auditor` reads.
    s = normalize_seat(s) or s
    # Crypto twin → resolve to its equity role for policy lookup. The
    # raw slot name is still recorded as `posted_as` so receipts/stances
    # can be sliced by lane.
    role_for_policy = s
    if s == "crypto":
        role_for_policy = "executor"
    elif s.startswith("crypto_"):
        role_for_policy = s[len("crypto_"):]
    p = SEAT_POLICY.get(role_for_policy)
    if not p:
        return {
            "posted_as": s,
            "may_decide": False,
            "may_execute": False,
            "may_veto": False,
        }
    return {
        "posted_as": s,
        "may_decide": p["may_decide"],
        "may_execute": p["may_execute"],
        "may_veto": p["may_veto"],
        "lane_scope": p.get("lane_scope"),
    }


def required_seats() -> tuple[str, ...]:
    """Seats whose silence triggers a degraded-quorum flag on positions."""
    return tuple(s for s, p in SEAT_POLICY.items() if p["seat_required"])


def seat_may_execute_lane(seat: str | None, lane: str | None) -> bool:
    """May the brain currently holding `seat` execute an intent in `lane`?

    Accepts BOTH the equity role name (`executor`) and the crypto slot
    name (`crypto`, `crypto_<role>`). Crypto slot names are resolved
    to their equity twin so the policy lookup finds the row; the
    LANE check is the real authority gate.

    Deprecated seat names (`decider`, `advisor`, `crypto_decider`,
    `crypto_advisor`) are run through `SEAT_ALIASES` first so old
    sidecars continue to function during the deprecation window.

    Fail-closed:
    - No seat → False
    - Seat policy says may_execute=False → False
    - Seat has a lane_scope and `lane` not in it → False
    - `lane` is None → False  (we never trade lane-untagged intents through scoped seats)
    """
    if not seat:
        return False
    s = (normalize_seat(seat.lower()) or seat.lower())
    role_for_policy = s
    if s == "crypto":
        role_for_policy = "executor"
        # Force lane_scope=["crypto"] regardless of the equity executor's scope.
        return (lane == "crypto") and bool(SEAT_POLICY.get("executor", {}).get("may_execute"))
    if s.startswith("crypto_"):
        # Non-execute crypto twins (crypto_governor, crypto_opponent,
        # crypto_auditor) never route orders. Fail-closed without
        # consulting the equity row's may_execute (which doesn't apply
        # to these advisory slots).
        return False
    p = SEAT_POLICY.get(role_for_policy)
    if not p or not p.get("may_execute"):
        return False
    scope = p.get("lane_scope")
    if scope is None:
        return True  # cross-lane execution rights
    if not lane:
        return False
    return lane in scope
