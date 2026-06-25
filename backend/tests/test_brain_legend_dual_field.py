"""Tests for the 2026-02-23 dual-field migration.

Pins the legend's `canonicalize_stack()` resolver against every
identity form the codebase accepts (canonical brain_id, legacy
stack code, lowercase display name, title-cased display name,
acronym-cased display name) and verifies the legend doc shape so
the operator-facing `/api/admin/brain-legend` route can't drift
silently.
"""
from __future__ import annotations

import pytest

from shared.brain_legend import (
    CANONICAL_BRAINS,
    DISPLAY_NAMES,
    LEGACY_TO_CANONICAL,
    canonicalize_stack,
    _legend_doc,
)


@pytest.mark.parametrize("raw,expected", [
    # Canonical brain_ids — pass through.
    ("camino", "camino"),
    ("barracuda", "barracuda"),
    ("hellcat", "hellcat"),
    ("gto", "gto"),
    # Legacy stack codes — resolve to canonical.
    ("alpha", "camino"),
    ("camaro", "barracuda"),
    ("chevelle", "hellcat"),
    ("redeye", "gto"),
    # Display names (title-case).
    ("Camino", "camino"),
    ("Barracuda", "barracuda"),
    ("Hellcat", "hellcat"),
    # Acronym display name.
    ("GTO", "gto"),
    ("gto", "gto"),
    # Whitespace + case tolerance.
    ("  CAMARO  ", "barracuda"),
    ("HELLCAT", "hellcat"),
    ("Alpha", "camino"),
    # Empty / falsy.
    ("", ""),
    (None, ""),
    # Unknown — return lowercased raw (defensive surface).
    ("ghostbrain", "ghostbrain"),
    ("RogueBrain", "roguebrain"),
])
def test_canonicalize_stack_resolves_every_known_form(raw, expected):
    assert canonicalize_stack(raw) == expected


def test_canonical_set_has_four_brains():
    assert CANONICAL_BRAINS == frozenset({
        "camino", "barracuda", "hellcat", "gto",
    })


def test_legacy_map_covers_all_four_brains():
    """Every canonical brain must have exactly one legacy alias.
    If we add a new brain, this test will surface the missing entry."""
    canonicals_via_legacy = set(LEGACY_TO_CANONICAL.values())
    assert canonicals_via_legacy == CANONICAL_BRAINS


def test_display_names_use_correct_casing():
    """GTO is an acronym (all-caps). Others are title-case."""
    assert DISPLAY_NAMES["gto"] == "GTO"
    assert DISPLAY_NAMES["camino"] == "Camino"
    assert DISPLAY_NAMES["barracuda"] == "Barracuda"
    assert DISPLAY_NAMES["hellcat"] == "Hellcat"


def test_legend_doc_shape_for_barracuda():
    """Pin the legend doc shape — the `/api/admin/brain-legend`
    route returns these fields, so removing/renaming one is a
    contract break that the UI would silently absorb."""
    doc = _legend_doc("barracuda")
    assert doc["canonical"] == "barracuda"
    assert doc["display_name"] == "Barracuda"
    assert doc["legacy_aliases"] == ["camaro"]
    assert "crypto_executor" in doc["doctrine_role"]
    assert "migrated_at" in doc
    assert "stack_canonical" in doc["migration_reason"]


def test_legend_doc_for_gto_uses_acronym_display_name():
    doc = _legend_doc("gto")
    assert doc["display_name"] == "GTO"
    assert doc["legacy_aliases"] == ["redeye"]
