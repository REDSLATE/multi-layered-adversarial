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


# ─── Seat aliases (2026-02-19; revised 2026-05-31 — canonical 8) ────
#
# Doctrine (operator pin, 2026-05-31):
#
#   The IP defines EXACTLY 8 seats. Anything outside this set is not
#   part of the IP and must alias back into the canonical set:
#
#     EQUITY LANE                CRYPTO LANE
#     ─────────────────────      ─────────────────────────
#     strategist                 crypto_strategist
#     executor                   crypto_executor (canonical: `crypto`)
#     governor    (Chev/Red)     crypto_governor (Chev/Red)
#     auditor                    crypto_auditor
#
#   Authority rules:
#     - A brain may hold ONE equity seat AND ONE crypto seat
#       simultaneously (a brain is allowed both lanes at once).
#     - Governor seats (equity + crypto) are RESTRICTED to Chevelle
#       and RedEye. All other seats — strategist, executor, auditor
#       and their crypto twins — are open to every brain by default
#       (including Chevelle and RedEye).
#     - `crypto` and `crypto_executor` are interchangeable names for
#       the crypto-executor slot. `crypto` is the legacy / historical
#       name; both resolve to the same seat policy row.
#
# Legacy aliases (pre-2026 names that still appear in old receipts):
#   decider          → executor          (2026-02-19 merge; the equity
#                                          decider role was reinstated
#                                          as `strategist` on 2026-05-24,
#                                          which carries may_decide=True)
#   crypto_decider   → crypto            (lane twin of `decider`)
#   advisor          → auditor           (2026-05-27 merge)
#   crypto_advisor   → crypto_auditor    (2026-05-27 merge)
#   opponent         → auditor           (2026-05-27 merge)
#   crypto_opponent  → crypto_auditor    (2026-05-27 merge)

SEAT_ALIASES: dict[str, str] = {
    # ── Symmetric crypto-executor name ────────────────────────────
    # `crypto_executor` is the doctrinally-symmetric name (mirrors
    # `executor`). `crypto` is the legacy slot name; both names point
    # at the same canonical seat row. New code may use either.
    "crypto_executor": "crypto",
    # ── Legacy compat ──────────────────────────────────────────────
    "decider":         "executor",
    "crypto_decider":  "crypto",
    "advisor":         "auditor",
    "crypto_advisor":  "crypto_auditor",
    "opponent":        "auditor",
    "crypto_opponent": "crypto_auditor",
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


# ─── Canonical seat policy (exactly 8 seats per operator doctrine) ──
#
# The crypto lane's policy rows mirror their equity twin's permissions
# but pin `lane_scope=["crypto"]` so seat authority is physically
# isolated between lanes. Doctrine: identity does not grant authority;
# neither does lane membership without seat occupancy.

SEAT_POLICY: dict[str, SeatPolicy] = {
    # ════════════════════════ EQUITY LANE ═════════════════════════
    "strategist": {
        # Forms the trust/reduce/veto/observation call. Carries
        # may_decide=True but NOT may_execute — strategist articulates
        # the trade thesis, executor moves the order.
        "may_decide": True,
        "may_execute": False,
        "may_veto": False,
        "seat_required": True,  # silence = no thesis
        "speaks_as": "strategist",
        "lane_scope": ["equity"],
    },
    "executor": {
        "may_decide": True,
        "may_execute": True,
        "may_veto": False,
        "seat_required": True,
        "speaks_as": "executor",
        "lane_scope": ["equity"],
    },
    "governor": {
        # Restricted by eligibility to Chevelle and RedEye only.
        "may_decide": False,
        "may_execute": False,
        "may_veto": True,
        # Governor silence = governance blindness. Operator must SEE
        # this loudly — that's how a governor going dark gets caught
        # before a bad call gets locked in.
        "seat_required": True,
        "speaks_as": "governor",
        "lane_scope": ["equity"],
    },
    "auditor": {
        # 2026-05-27 — Auditor ABSORBED the opponent role. Two
        # doctrinal jobs across the trade lifecycle:
        #   (1) PRE-TRADE — argues the contrary case (formerly opponent)
        #   (2) POST-TRADE — analyzes outcome vs intent on closed positions
        # Both are skeptical / critical roles off the execution path.
        # An empty auditor seat means MC silently loses both voices.
        "may_decide": False,
        "may_execute": False,
        "may_veto": False,
        "seat_required": True,
        "speaks_as": "auditor",
        "lane_scope": ["equity"],
    },

    # ════════════════════════ CRYPTO LANE ═════════════════════════
    "crypto_strategist": {
        # Crypto twin of strategist — forms the crypto trade thesis.
        "may_decide": True,
        "may_execute": False,
        "may_veto": False,
        "seat_required": True,
        "speaks_as": "strategist",
        "lane_scope": ["crypto"],
    },
    "crypto": {
        # Canonical crypto-executor seat. Aliased name:
        # `crypto_executor`. Observe, buy, sell crypto. No equity
        # rights. Lane is physically isolated from `executor`.
        "may_decide": False,
        "may_execute": True,
        "may_veto": False,
        "seat_required": True,
        "speaks_as": "crypto",
        "lane_scope": ["crypto"],
    },
    "crypto_governor": {
        # Restricted by eligibility to Chevelle and RedEye only.
        "may_decide": False,
        "may_execute": False,
        "may_veto": True,
        "seat_required": True,
        "speaks_as": "governor",
        "lane_scope": ["crypto"],
    },
    "crypto_auditor": {
        # Crypto twin of the merged auditor seat — same pre-trade-
        # opponent + post-trade-review doctrine, scoped to crypto.
        "may_decide": False,
        "may_execute": False,
        "may_veto": False,
        "seat_required": True,
        "speaks_as": "auditor",
        "lane_scope": ["crypto"],
    },
}

# CANONICAL_SEATS is the IP boundary. Anything not in this tuple is
# either an alias (see SEAT_ALIASES) or not part of the IP. The
# assertion guards against accidental schema drift in this file.
CANONICAL_SEATS: tuple[str, ...] = (
    "strategist", "executor", "governor", "auditor",
    "crypto_strategist", "crypto", "crypto_governor", "crypto_auditor",
)
assert set(CANONICAL_SEATS) == set(SEAT_POLICY.keys()), (
    "CANONICAL_SEATS drifted from SEAT_POLICY — IP boundary violated. "
    f"policy={sorted(SEAT_POLICY.keys())} canonical={sorted(CANONICAL_SEATS)}"
)
assert len(CANONICAL_SEATS) == 8, (
    f"CANONICAL_SEATS must contain exactly 8 seats per IP doctrine, "
    f"found {len(CANONICAL_SEATS)}"
)

# All recognized seat names (= the canonical 8).
SEATS: tuple[str, ...] = CANONICAL_SEATS


def snapshot(seat: str | None) -> dict:
    """Snapshot the policy for the given seat. Returns an empty-permission
    record when the brain holds no seat — that's the safest default.

    The seat string is normalized to lowercase and run through the
    deprecation alias table (`SEAT_ALIASES`). The 8 canonical seats are
    looked up directly. Aliases:
      - `crypto_executor` → `crypto`  (symmetric naming)
      - `decider` → `executor`        (legacy 2026-02-19 merge)
      - `advisor`, `opponent` → `auditor` (legacy 2026-05-27 merge)
      - Their `crypto_*` twins same.

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
    # Apply alias normalization before lookup.
    s = normalize_seat(s) or s
    p = SEAT_POLICY.get(s)
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

    Direct lookup via the canonical 8-seat policy:
      - executor          → may execute equity only
      - crypto (aka crypto_executor) → may execute crypto only
      - All other seats   → fail-closed (no execution rights)

    Deprecated seat names (`decider`, `advisor`, `opponent`, their
    `crypto_*` twins) are run through `SEAT_ALIASES` first so old
    sidecars continue to function during the deprecation window.

    Fail-closed:
    - No seat → False
    - Seat policy says may_execute=False → False
    - Seat has a lane_scope and `lane` not in it → False
    - `lane` is None → False (never trade lane-untagged intents)
    """
    if not seat:
        return False
    s = normalize_seat(seat.lower()) or seat.lower()
    p = SEAT_POLICY.get(s)
    if not p or not p.get("may_execute"):
        return False
    scope = p.get("lane_scope")
    if scope is None:
        # Canonical 8 all have explicit lane scope; this branch only
        # fires for unknown/extended seats — fail-closed.
        return False
    if not lane:
        return False
    return lane in scope
