"""Regression test for the 2026-06-18 Prod "seat: opponent · holder: vacant"
bug.

Symptom: on `https://mission.risedual.ai` the Intents page's expanded
doctrine strip showed every intent's STRATEGIST / AUDITOR seat as
`holder: vacant` even though the operator had assigned all 8 seats via
QSS. Governor was fine.

Root cause: `EQUITY_SEAT_MAP` in `strategy_doctrines.py` and
`large_cap_doctrine.py` still mapped `strategist → "decider"` and
`adversary → "opponent"` (the legacy 2024-era seat names). But
`fetch_seat_holders()` returns the holder dict keyed by CANONICAL
seat names (`strategist`, `auditor`, `governor`, `executor`). So
every `holders.get(EQUITY_SEAT_MAP["strategist"])` call resolved
to `holders.get("decider")` → None, painting "vacant" in the UI.

The fix mirrors what `shared/doctrine/brain_sidecars.py` already had:
canonical seat keys in `EQUITY_SEAT_MAP` so role-to-seat lookups
return real holders.

This test locks the invariant: every doctrine packet builder's seat
map must address the canonical roster keys.
"""
from shared.doctrine.brain_sidecars import EQUITY_SEAT_MAP as BS_MAP
from shared.doctrine.large_cap_doctrine import EQUITY_SEAT_MAP as LC_MAP
from shared.doctrine.strategy_doctrines import EQUITY_SEAT_MAP as ST_MAP
from shared.crypto.doctrine.crypto_brain_sidecars import CRYPTO_SEAT_MAP

# Canonical seat names per the 8-seat IP (matches `fetch_seat_holders`
# return shape and the roster's `assignments` keys).
CANONICAL_EQUITY = {"strategist", "auditor", "governor", "executor"}
CANONICAL_CRYPTO = {"crypto_strategist", "crypto_auditor", "crypto_governor", "crypto"}

# Legacy keys that MUST NOT appear as values in any doctrine packet's
# seat map. Each one cost us a real prod incident.
LEGACY_BANNED = {"decider", "opponent", "advisor", "crypto_decider", "crypto_opponent"}


def _assert_canonical(map_name: str, seat_map: dict, allowed: set[str]) -> None:
    for role, seat in seat_map.items():
        assert seat in allowed, (
            f"{map_name}[{role!r}] = {seat!r} — not in canonical roster keys "
            f"{sorted(allowed)}. fetch_seat_holders() would return None for "
            f"this seat and the UI would show 'holder: vacant'."
        )
        assert seat not in LEGACY_BANNED, (
            f"{map_name}[{role!r}] = {seat!r} — that's a LEGACY name. "
            f"Use the canonical name from the 8-seat IP."
        )


def test_brain_sidecars_uses_canonical_equity_seats():
    _assert_canonical("brain_sidecars.EQUITY_SEAT_MAP", BS_MAP, CANONICAL_EQUITY)


def test_large_cap_uses_canonical_equity_seats():
    # This was failing in 2026-06-18 Prod — see module docstring.
    _assert_canonical("large_cap_doctrine.EQUITY_SEAT_MAP", LC_MAP, CANONICAL_EQUITY)


def test_strategy_doctrines_uses_canonical_equity_seats():
    # This was failing in 2026-06-18 Prod — see module docstring.
    _assert_canonical("strategy_doctrines.EQUITY_SEAT_MAP", ST_MAP, CANONICAL_EQUITY)


def test_crypto_brain_sidecars_uses_canonical_crypto_seats():
    _assert_canonical("crypto_brain_sidecars.CRYPTO_SEAT_MAP", CRYPTO_SEAT_MAP, CANONICAL_CRYPTO)


def test_all_three_equity_maps_agree():
    """All three equity packet builders must define the same role→seat
    mapping. If they drift, two different intents with the same lane
    can show different seat labels in the UI for the same logical
    role — confusing for an operator auditing trade decisions."""
    assert BS_MAP == LC_MAP == ST_MAP, (
        f"Equity seat maps disagree:\n"
        f"  brain_sidecars:    {BS_MAP}\n"
        f"  large_cap:         {LC_MAP}\n"
        f"  strategy_doctrines:{ST_MAP}"
    )
