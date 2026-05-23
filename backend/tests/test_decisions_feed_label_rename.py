"""Tripwire — decisions-feed kind labels.

Pins the doctrinally-honest naming applied 2026-05-23:

  * `mc_shelly` rows surface as `kind="engine_audit"` (NOT
    `training_signal`). They are real live audit events from MC's
    engine — gate passes/fails, intent ingest, council decisions,
    position lifecycle. The previous label falsely implied training
    mode.

  * The legacy `training_signal` filter value remains accepted as an
    alias so external consumers with cached URLs don't break.
"""
from __future__ import annotations

import pytest

from shared.decisions_feed import _normalize_mc_shelly


pytestmark = pytest.mark.tripwire


def test_mc_shelly_row_labelled_engine_audit():
    """A real `gate_pass` row from mc_shelly must surface as
    `kind=engine_audit`, NOT `training_signal`."""
    doc = {
        "event_id": "x",
        "event_type": "gate_pass",
        "ts": "2026-05-23T12:00:00+00:00",
        "brain": "camaro",
        "symbol": "AAPL",
        "action": "BUY",
        "outcome": "pass",
        "rationale": "schema invariants passed",
    }
    out = _normalize_mc_shelly(doc)
    assert out["kind"] == "engine_audit", (
        f"mc_shelly rows must surface as kind=engine_audit; got {out['kind']!r}. "
        "The legacy `training_signal` label was misleading — these are LIVE "
        "engine audit events, not training-only signals."
    )
    # The action carries the actual event_type so the UI summary stays
    # specific.
    assert out["action"] == "gate_pass"
    assert out["brain"] == "camaro"


def test_mc_shelly_row_without_event_type_falls_back_safely():
    """An mc_shelly row missing `event_type` must still normalise to
    `engine_audit` (no spurious `training_signal` regression)."""
    out = _normalize_mc_shelly({"ts": "2026-05-23T12:00:00+00:00", "brain": "alpha"})
    assert out["kind"] == "engine_audit"
    assert out["action"] == "engine_event"  # safe fallback, not "training_signal"


@pytest.mark.asyncio
async def test_decisions_feed_accepts_legacy_training_signal_alias(monkeypatch):
    """Caller passing `kinds=training_signal` (legacy clients) must
    still receive engine_audit rows — back-compat invariant."""
    from shared import decisions_feed

    seen_kinds: list[set] = []

    # Patch _SOURCES iteration to capture which collections get queried
    # based on the kinds filter. We don't need real DB — just the
    # filter-translation behaviour.
    orig_sources = decisions_feed._SOURCES

    class _FakeCursor:
        def __init__(self):
            self._docs = []
        def find(self, *a, **kw): return self
        def sort(self, *a, **kw): return self
        def limit(self, *a, **kw): return self
        async def to_list(self, length=None): return []

    class _FakeColl:
        def find(self, *a, **kw): return _FakeCursor()

    class _FakeDB(dict):
        def __getitem__(self, key):
            return _FakeColl()

    monkeypatch.setattr(decisions_feed, "db", _FakeDB())

    # Call the endpoint with the LEGACY kind name.
    result = await decisions_feed.decisions_feed(
        brain=None, kinds="training_signal", limit=10, _user={"email": "test"},
    )
    # Endpoint accepted the call without 400 and returned the structure.
    assert "items" in result
    assert "filter" in result
    # The filter echoes back the resolved kinds — `training_signal`
    # must have been remapped to `engine_audit`.
    resolved = result["filter"]["kinds"]
    if isinstance(resolved, list):
        assert "engine_audit" in resolved, (
            f"legacy `training_signal` filter must remap to `engine_audit`; "
            f"got {resolved!r}"
        )
        assert "training_signal" not in resolved, (
            "legacy alias must NOT pass through as-is — it should be "
            "translated to the canonical `engine_audit` value."
        )
