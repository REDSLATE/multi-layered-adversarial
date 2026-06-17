"""Single source of truth for brain identity canonicalization.

Doctrine (2026-06-09 — operator directive after the AAPL saturation
post-mortem): "Names can absolutely hinder them if routing, seats,
readiness, or executor checks depend on names instead of canonical
IDs. Display names = UI only. Canonical IDs = routing/execution only.
Roles = seat logic only."

This module is the choke point. Every code path that converts a
human-typed or LLM-emitted brain reference into something used for
routing, DB collection naming, seat policy, or execution authority
MUST funnel through `normalize_brain_id` first. The fail-mode of NOT
funnelling is silent — a typo or display-name leak produces an
orphan Mongo collection or a route that misses, with no exception.

What we lock down:
  * Canonical IDs are LOWERCASE: `alpha / camaro / chevelle / redeye`.
    They are MC's primary keys, used in:
      - `shared_intents.stack`           (Pydantic-validated)
      - `shared_intents.brain`           (audit metadata)
      - roster `assignments[role]`
      - learning_ladder `(brain, lane)` pairs
      - `shelly_<brain>_*` collection names
      - `LIVE_RUNTIMES`, `RUNTIMES`, `BRAINS` constants
  * Display names are TITLE CASE proper nouns:
    `Camino / Barracuda / Hellcat / GTO`.
    They are surfaced ONLY in:
      - UI labels (frontend `RUNTIME_META.label`)
      - intent rationale strings ("display_name=Barracuda ...")
      - audit log human-readable fields
    Never used for routing, dispatch, or storage key derivation.

Anti-pattern this module exists to prevent:
    if intent["brain"] == "Camaro":          # ← BUG: display name as key
    coll_name = f"shelly_{brain_name}_..."   # ← BUG: untrusted input
                                             #   becomes a collection name

Correct pattern:
    from shared.brain_identity import normalize_brain_id
    brain_id = normalize_brain_id(intent.get("brain") or intent.get("display_name"))
    if brain_id == "camaro":
        ...
"""
from __future__ import annotations

from typing import Final


# Canonical lowercase IDs. The 2026-02-20 rename collapses
# alpha→camino, camaro→barracuda, chevelle→hellcat, redeye→gto so the
# DB / code IDs match the operator-facing display names. Legacy IDs
# are still accepted at ingress (see DISPLAY_TO_ID below) so historical
# rows / external integrations don't break — but every new write uses
# the canonical names below.
VALID_BRAIN_IDS: Final[frozenset[str]] = frozenset({
    "camino", "barracuda", "hellcat", "gto",
})

# Legacy → canonical. The pre-rename canonical names get aliased to
# the new canonical so any historical doc, audit row, or third-party
# integration that still emits `"redeye"` keeps working.
LEGACY_TO_CANONICAL: Final[dict[str, str]] = {
    "alpha":    "camino",
    "camaro":   "barracuda",
    "chevelle": "hellcat",
    "redeye":   "gto",
}

# Display → canonical. Accepts the new operator-facing names in any
# casing variant, plus the legacy slot codes for back-compat.
DISPLAY_TO_ID: Final[dict[str, str]] = {
    # Current canonical (display name == canonical ID, title-cased)
    "Camino":    "camino",
    "Barracuda": "barracuda",
    "Hellcat":   "hellcat",
    "GTO":       "gto",
    # Casing variants
    "CAMINO":    "camino",
    "BARRACUDA": "barracuda",
    "HELLCAT":   "hellcat",
    "Gto":       "gto",
    "gto":       "gto",
    "camino":    "camino",
    "barracuda": "barracuda",
    "hellcat":   "hellcat",
    # Legacy slot codes — accepted at ingress, mapped to new canonical
    "Alpha":     "camino",
    "alpha":     "camino",
    "ALPHA":     "camino",
    "Camaro":    "barracuda",
    "camaro":    "barracuda",
    "CAMARO":    "barracuda",
    "Chevelle":  "hellcat",
    "chevelle":  "hellcat",
    "CHEVELLE":  "hellcat",
    "RedEye":    "gto",
    "Redeye":    "gto",
    "redeye":    "gto",
    "REDEYE":    "gto",
    "Red Eye":   "gto",
}

# Sentinel returned when the input cannot be canonicalized. Caller
# code should treat this as "unknown brain — refuse to route" rather
# than silently substituting a valid brain. NEVER falls through to
# a default brain — silent substitution would re-introduce exactly
# the routing-misdirection bug this module prevents.
UNKNOWN_BRAIN: Final[str] = "unknown"


def normalize_brain_id(value: object) -> str:
    """Resolve any brain reference (canonical ID, display name, or
    casing variant) to its canonical lowercase ID.

    Resolution order (first match wins):
      1. Already a canonical ID (after lowercase strip).
      2. Exact match in `DISPLAY_TO_ID`.
      3. Returns `UNKNOWN_BRAIN` (the literal string `"unknown"`).

    The function never raises and never silently substitutes a
    "default" brain — unknown inputs are surfaced explicitly so the
    caller can fail-closed (refuse to route) rather than fail-open
    (route to whatever the default was).

    Args:
        value: Any value. Non-strings are coerced via `str()` for
               permissive intake, then stripped of whitespace.

    Returns:
        One of `VALID_BRAIN_IDS` if recognisable, otherwise
        `UNKNOWN_BRAIN`.

    Examples:
        >>> normalize_brain_id("Barracuda")
        'camaro'
        >>> normalize_brain_id("CAMARO")
        'camaro'
        >>> normalize_brain_id("camaro")
        'camaro'
        >>> normalize_brain_id("RedEye")
        'redeye'
        >>> normalize_brain_id("nonsense")
        'unknown'
        >>> normalize_brain_id(None)
        'unknown'
        >>> normalize_brain_id("")
        'unknown'
    """
    if value is None:
        return UNKNOWN_BRAIN
    text = str(value).strip()
    if not text:
        return UNKNOWN_BRAIN

    lowered = text.lower()
    # Already canonical?
    if lowered in VALID_BRAIN_IDS:
        return lowered
    # Legacy canonical → new canonical.
    if lowered in LEGACY_TO_CANONICAL:
        return LEGACY_TO_CANONICAL[lowered]

    if text in DISPLAY_TO_ID:
        return DISPLAY_TO_ID[text]

    # One last permissive lookup: a casing variant the DISPLAY_TO_ID
    # table didn't enumerate (e.g., "barracuda" lowercase typed by
    # the operator). This is safe — we only match against known
    # display names, not against arbitrary substrings.
    for display, canonical in DISPLAY_TO_ID.items():
        if display.lower() == lowered:
            return canonical

    return UNKNOWN_BRAIN


def is_known_brain(value: object) -> bool:
    """Return True iff `value` resolves to a canonical brain. Useful
    for fail-closed guards at routing boundaries."""
    return normalize_brain_id(value) != UNKNOWN_BRAIN


__all__ = [
    "VALID_BRAIN_IDS",
    "DISPLAY_TO_ID",
    "LEGACY_TO_CANONICAL",
    "UNKNOWN_BRAIN",
    "normalize_brain_id",
    "is_known_brain",
]
