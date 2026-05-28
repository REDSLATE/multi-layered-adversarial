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


# ─── Seat aliases (2026-02-19; revised 2026-05-24) ──────────────────
#
# Doctrine:
#
#   2026-05-24 — Operator renamed the equity `decider` role to
#   `strategist`. The seat function is unchanged (form the
#   trust/reduce/veto/observation call). For policy lookup it still
#   resolves to the same row as `executor` (which already carries
#   may_decide=True) — but the ROSTER SLOT is a distinct seat occupied
#   by a different brain.
#
#   AUDITOR was revived as a real seat (2026-05-24). Under the prior
#   PARADOX hierarchy, auditor was treated as an emergent artifact;
#   the operator restored it as a roster slot for post-trade review.
#
#   The aliases let old sidecars that still send legacy seat names keep
#   working — MC normalizes to the canonical name at every boundary.

SEAT_ALIASES: dict[str, str] = {
    # ── Legacy compat ──────────────────────────────────────────────
    # 2026-02-19: `decider`/`crypto_decider` were merged into `executor`/
    # `crypto`. 2026-05-24: the equity seat was reinstated as a distinct
    # role under the name `strategist` (see SEAT_POLICY below). Legacy
    # sidecars still posting `decider` keep resolving to `executor`'s
    # policy row for pre-rename receipt forensics; new code uses
    # `strategist` directly.
    "decider":         "executor",
    "crypto_decider":  "crypto",
    # 2026-05-27: opponent merged into auditor. Legacy advisory refs
    # now resolve to auditor as well (same end state: skeptical voice).
    "advisor":         "auditor",
    "crypto_advisor":  "crypto_auditor",
    # 2026-05-27: opponent seat merged into auditor. Any code path
    # still referencing `opponent`/`crypto_opponent` resolves to the
    # auditor seat policy row.
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


SEAT_POLICY: dict[str, SeatPolicy] = {
    "strategist": {
        # 2026-05-24: Reinstated as a distinct equity seat (was the old
        # `decider` slot, briefly merged into `executor`). Forms the
        # trust/reduce/veto/observation call. Carries may_decide=True
        # but NOT may_execute — strategist articulates the trade thesis,
        # executor moves the order. Lane: equity only.
        "may_decide": True,
        "may_execute": False,
        "may_veto": False,
        "seat_required": True,  # strategist silence on a position = no thesis
        "speaks_as": "strategist",
        "lane_scope": ["equity"],
    },
    "decider": {
        # DEPRECATED — alias rewrites `decider` → `executor` for legacy
        # sidecars. Kept in the policy table so historical receipts that
        # recorded `seat=decider` resolve to a sensible row. New code
        # uses `strategist` (above).
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
        # 2026-05-27: DEPRECATED — opponent seat merged into auditor.
        # This entry is retained for legacy code paths that still read
        # `SEAT_POLICY["opponent"]` directly (forensic audit, intent
        # receipts predating the merge). All NEW roster assignments
        # for `opponent` are alias-rewritten to `auditor` via
        # `_LEGACY_ROLE_REWRITES` in roster.py and `SEAT_ALIASES` above.
        # The policy row mirrors `auditor` so downstream readers see
        # the same permissions either way.
        "may_decide": False,
        "may_execute": False,
        "may_veto": False,
        "seat_required": True,  # same as auditor — silence = adversarial blindness
        "speaks_as": "auditor",
        "lane_scope": None,
    },
    "auditor": {
        # 2026-05-27 — Auditor ABSORBED the opponent role. The seat now
        # carries TWO doctrinal jobs across the trade lifecycle:
        #   (1) PRE-TRADE — argues the contrary case (formerly opponent)
        #   (2) POST-TRADE — analyzes outcome vs intent on closed positions
        # Both are skeptical/critical roles that sit OFF the execution
        # path. Combining them gives the brain that wrote the
        # pre-mortem the natural seat to write the post-mortem —
        # closes the learning loop. No deciding, no executing, no
        # vetoing. `seat_required=True` (inherited from opponent): an
        # empty auditor seat means MC silently loses both the
        # contrary-case voice AND the post-trade review. Lane: None
        # (general — operator can assign per-lane via `crypto_auditor`).
        "may_decide": False,
        "may_execute": False,
        "may_veto": False,
        "seat_required": True,
        "speaks_as": "auditor",
        "lane_scope": None,
    },
    "crypto_auditor": {
        # 2026-05-27: crypto twin of the merged auditor seat. Same
        # pre-trade-opponent + post-trade-review doctrine, scoped to
        # the crypto lane.
        "may_decide": False,
        "may_execute": False,
        "may_veto": False,
        "seat_required": True,
        "speaks_as": "auditor",
        "lane_scope": ["crypto"],
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

    Aliases (2026-05-27): `opponent`→`auditor`, `crypto_opponent`→
    `crypto_auditor` (opponent seat merged into auditor). Also
    (2026-02-19): `decider`→`executor`, `advisor`→`auditor`, and their
    `crypto_*` twins. Old sidecars sending the deprecated seat names
    still resolve to a working policy without behavioral change
    because the alias targets carry the responsibilities the old
    names tried to express.

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
