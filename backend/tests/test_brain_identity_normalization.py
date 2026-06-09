"""Tests for the brain identity normalization layer.

Pins the doctrine the operator established 2026-06-09: display names
are UI-only; canonical IDs are routing/storage-only. Any code path
that converts a brain reference into a routing key, DB collection
name, or seat lookup MUST funnel through `normalize_brain_id`.

The tests cover three categories:
  1. Canonical-input pass-through (idempotency).
  2. Display-name → canonical-ID translation for every known brand
     label + every documented casing variant.
  3. Fail-closed behaviour on unknown input — must return
     `UNKNOWN_BRAIN`, never a default brain (silent substitution
     would re-introduce the routing-misdirection bug).
"""
from __future__ import annotations

import pytest

from shared.brain_identity import (
    DISPLAY_TO_ID,
    UNKNOWN_BRAIN,
    VALID_BRAIN_IDS,
    is_known_brain,
    normalize_brain_id,
)


CANONICAL_IDS = ("alpha", "camaro", "chevelle", "redeye")


# ── Pass-through (idempotency) ────────────────────────────────────


@pytest.mark.parametrize("canonical_id", CANONICAL_IDS)
def test_canonical_id_passes_through_unchanged(canonical_id):
    """Calling normalize on a canonical ID returns the same canonical
    ID. This is the hot-path case — every production caller is
    expected to already pass canonical IDs."""
    assert normalize_brain_id(canonical_id) == canonical_id


@pytest.mark.parametrize("canonical_id", CANONICAL_IDS)
def test_canonical_id_uppercased_still_canonical(canonical_id):
    """Case-insensitive on canonical IDs too — `CAMARO` resolves to
    `camaro`, not `unknown`."""
    assert normalize_brain_id(canonical_id.upper()) == canonical_id


# ── Display name → canonical translation ──────────────────────────


@pytest.mark.parametrize(
    "display,expected",
    [
        ("Camino", "alpha"),
        ("Barracuda", "camaro"),
        ("Hellcat", "chevelle"),
        ("GTO", "redeye"),
    ],
)
def test_operator_brand_names_resolve_to_canonical(display, expected):
    """The four brand names the operator established (2026-06-09)
    must all resolve to their slot IDs."""
    assert normalize_brain_id(display) == expected


@pytest.mark.parametrize(
    "variant,expected",
    [
        ("CAMINO", "alpha"),       # all-caps
        ("barracuda", "camaro"),   # all-lowercase
        ("HellCat", "chevelle"),   # mixed case
        ("gto", "redeye"),         # all-lowercase initialism
        ("Gto", "redeye"),         # title-case initialism
        ("RedEye", "redeye"),      # CamelCase legacy variant
        ("Red Eye", "redeye"),     # two-word legacy variant
    ],
)
def test_casing_variants_resolve(variant, expected):
    """Operator / LLM-emitted casing variants must not break routing.
    Defence against typos and case drift."""
    assert normalize_brain_id(variant) == expected


@pytest.mark.parametrize(
    "legacy_label,expected",
    [
        ("Alpha", "alpha"),        # slot code typed as display
        ("Camaro", "camaro"),
        ("Chevelle", "chevelle"),
        ("Redeye", "redeye"),
    ],
)
def test_legacy_slot_labels_resolve(legacy_label, expected):
    """When previous-pass UI bundles cached on operator devices still
    emit `Alpha / Camaro / Chevelle / Redeye` as display labels,
    those must still route correctly — defence-in-depth for the
    rename event itself."""
    assert normalize_brain_id(legacy_label) == expected


# ── Fail-closed on unknown input ──────────────────────────────────


@pytest.mark.parametrize(
    "garbage",
    ["", "   ", None, "Mustang", "alphaaa", "xyz", "barracudaaa", "🦈"],
)
def test_unknown_input_returns_unknown_not_default(garbage):
    """Unknown input MUST resolve to `UNKNOWN_BRAIN`. The function
    is forbidden from substituting a "default" brain when the input
    is unrecognised — silent substitution is exactly the routing
    bug this module exists to prevent."""
    assert normalize_brain_id(garbage) == UNKNOWN_BRAIN


def test_unknown_brain_constant_is_a_sentinel():
    """`UNKNOWN_BRAIN` must not collide with any canonical ID."""
    assert UNKNOWN_BRAIN not in VALID_BRAIN_IDS


def test_is_known_brain_helper_matches_normalize():
    """`is_known_brain` is just a sugar wrapper over `normalize` !=
    UNKNOWN. Verify they never disagree."""
    samples = ["alpha", "Barracuda", "Hellcat", "GTO", "RedEye",
               "garbage", "", None, "CAMARO"]
    for s in samples:
        assert is_known_brain(s) == (normalize_brain_id(s) != UNKNOWN_BRAIN)


# ── DISPLAY_TO_ID table integrity ─────────────────────────────────


def test_display_to_id_map_only_targets_canonical_ids():
    """Every value in DISPLAY_TO_ID must be a canonical ID. Otherwise
    we'd be silently producing fake brain IDs."""
    for display, target in DISPLAY_TO_ID.items():
        assert target in VALID_BRAIN_IDS, (
            f"DISPLAY_TO_ID[{display!r}] = {target!r} is not a canonical ID"
        )


def test_every_canonical_id_is_a_target():
    """Every canonical ID must be reachable from at least one display
    name in the table — otherwise a brain with no display label is
    secretly orphaned from UI lookups."""
    targets = set(DISPLAY_TO_ID.values())
    assert VALID_BRAIN_IDS <= targets


# ── Integration: LocalShelly uses normalization ───────────────────


def test_local_shelly_normalizes_display_names_to_canonical_collections():
    """The one risk surface the audit found: `LocalShelly` accepting
    any string and using it as a Mongo collection suffix. After the
    fix, passing `Barracuda` must land in the canonical
    `shelly_camaro_memories` collection, not an orphan
    `shelly_barracuda_memories`."""
    from shelly.local_shelly import LocalShelly

    s_display = LocalShelly("Barracuda")
    s_canonical = LocalShelly("camaro")
    s_uppercase = LocalShelly("CAMARO")

    assert s_display.brain_name == "camaro"
    assert s_canonical.brain_name == "camaro"
    assert s_uppercase.brain_name == "camaro"
    assert s_display.memories_coll_name == "shelly_camaro_memories"
    assert s_display.receipts_coll_name == "shelly_camaro_reasoning_receipts"


def test_local_shelly_preserves_non_canonical_test_fixture_names():
    """Edge case: existing tests use names like `twembed` that are
    intentionally non-canonical. The normalization layer must NOT
    rewrite those — production canonical names are normalized,
    everything else passes through (lowercased) for back-compat."""
    from shelly.local_shelly import LocalShelly

    s = LocalShelly("twembed")
    assert s.brain_name == "twembed"
    assert s.memories_coll_name == "shelly_twembed_memories"
