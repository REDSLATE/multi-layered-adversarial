"""Position context injection (audit row #7) — brains see current
holdings via `snapshot.position_context[self.id]`, never through
their own Mongo lookups."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from mc_pulse.snapshot import build_snapshot


def test_position_context_defaults_to_empty_mapping():
    s = build_snapshot(
        symbol="NVDA", lane="equity",
        timestamp=datetime(2026, 7, 11, 14, 30, tzinfo=timezone.utc),
        price=Decimal("140.50"), indicators={},
    )
    assert s.position_context == {}
    # Immutable — brains cannot inject a position.
    with pytest.raises(TypeError):
        s.position_context["camino"] = {"side": "LONG"}  # type: ignore[index]


def test_position_context_preserves_brain_map():
    s = build_snapshot(
        symbol="NVDA", lane="equity",
        timestamp=datetime(2026, 7, 11, 14, 30, tzinfo=timezone.utc),
        price=Decimal("140.50"), indicators={},
        position_context={
            "camino": {"direction": "LONG", "signed_qty": 100},
            "barracuda": {"direction": "SHORT", "signed_qty": -50},
        },
    )
    assert s.position_context["camino"]["direction"] == "LONG"
    assert s.position_context["barracuda"]["signed_qty"] == -50
    # Immutable at the top level (mutating a nested dict is a
    # design boundary — brains are trusted not to write into
    # their own nested dict; the top-level map is the audit
    # anchor).
    with pytest.raises(TypeError):
        s.position_context["hellcat"] = {}  # type: ignore[index]


def test_position_context_read_only_get():
    s = build_snapshot(
        symbol="NVDA", lane="equity",
        timestamp=datetime(2026, 7, 11, 14, 30, tzinfo=timezone.utc),
        price=Decimal("140.50"), indicators={},
        position_context={"camino": {"direction": "LONG"}},
    )
    # Standard read API works.
    assert s.position_context.get("camino") == {"direction": "LONG"}
    assert s.position_context.get("gto") is None
    assert list(s.position_context.keys()) == ["camino"]
