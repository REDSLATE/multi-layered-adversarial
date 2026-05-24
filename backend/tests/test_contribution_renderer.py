"""Tripwire — contribution renderer reads top-level audit fields (2026-05-24).

Doctrine pin: the unified decisions feed renderer used to demand a
`payload` dict carrying `symbol/side/confidence` on contribution rows.
That conflated two different surfaces — contributions are PERIODIC
GLOBAL-STATE snapshots, not per-symbol opinions. The renderer now
reads contribution data off the audit-row top level (mode / notes /
weights / recent_outcomes / delta_reason / confidence_delta) and
extracts a display anchor (symbol/action/confidence) from
recent_outcomes[0] only as presentation, never as a schema requirement.
"""
from __future__ import annotations

import pytest

from shared.decisions_feed import _normalize_sovereign


pytestmark = pytest.mark.tripwire


def test_substantive_contribution_does_not_show_skeleton_tag():
    """A contribution carrying weights + recent_outcomes must NOT be
    flagged as empty/skeleton in the rendered summary."""
    doc = {
        "ts": "2026-05-24T10:00:00+00:00",
        "brain": "redeye",
        "action": "contribution",
        "posted_as": "opponent",
        "mode": "DTD",
        "weights": {"trend": 0.5, "macd": -0.2},
        "recent_outcomes": [
            {"symbol": "BTC/USD", "action": "SELL", "confidence": 0.72, "outcome": 1},
        ],
        "recent_outcomes_count": 1,
        "notes": "post-VIX-spike review",
        "has_substance": True,
    }
    out = _normalize_sovereign(doc)
    assert "(no substance" not in out["summary"]
    assert "(empty payload)" not in out["summary"]


def test_contribution_extracts_display_anchor_from_recent_outcomes():
    """When recent_outcomes is populated, the renderer surfaces the
    most-recent symbol/action/conf as a DISPLAY ANCHOR. The contribution
    schema is NOT being changed — this is presentation only."""
    doc = {
        "ts": "2026-05-24T10:00:00+00:00",
        "brain": "redeye",
        "action": "contribution",
        "posted_as": "opponent",
        "mode": "DTD",
        "recent_outcomes": [
            {"symbol": "ETH/USD", "action": "BUY", "confidence": 0.84, "outcome": 1},
        ],
        "has_substance": True,
    }
    out = _normalize_sovereign(doc)
    s = out["summary"]
    assert "ETH/USD" in s, f"display anchor symbol missing from summary: {s!r}"
    assert "BUY" in s, f"display anchor side missing from summary: {s!r}"
    assert "0.84" in s, f"display anchor confidence missing from summary: {s!r}"
    # The symbol also propagates to the top-level field so the feed
    # filterable column works.
    assert out["symbol"] == "ETH/USD"


def test_contribution_summary_includes_substance_signals():
    """The summary shows mode / outcomes count / weights count / notes."""
    doc = {
        "ts": "2026-05-24T10:00:00+00:00",
        "brain": "redeye",
        "action": "contribution",
        "posted_as": "opponent",
        "mode": "DTD",
        "weights": {"a": 1.0, "b": 0.5, "c": -0.2},
        "recent_outcomes": [
            {"symbol": "X", "action": "BUY", "confidence": 0.5, "outcome": 0},
            {"symbol": "Y", "action": "SELL", "confidence": 0.7, "outcome": 1},
        ],
        "recent_outcomes_count": 2,
        "notes": "test note",
        "has_substance": True,
    }
    s = _normalize_sovereign(doc)["summary"]
    assert "mode=DTD" in s
    assert "outcomes=2" in s
    assert "weights=3" in s
    assert "test note" in s


def test_legacy_pregate_contribution_flagged():
    """Historical rows with no substance still get flagged so the
    operator sees them — but with the cleaner "(no substance — pre-gate
    row)" wording instead of the misleading "no symbol/side/conf"."""
    doc = {
        "ts": "2026-05-20T10:00:00+00:00",
        "brain": "redeye",
        "action": "contribution",
        "posted_as": "opponent",
        "mode": "DTD",
        # Truly empty — no notes, no weights, no outcomes.
        "has_substance": False,
    }
    s = _normalize_sovereign(doc)["summary"]
    assert "no substance" in s
    # The old misleading text MUST be gone.
    assert "no symbol/side/conf" not in s


def test_non_contribution_sovereign_row_uses_original_logic():
    """Non-contribution sovereign rows (e.g., promotions, mode changes)
    follow the original behaviour: show symbol/side/conf if present,
    flag empty payload if not. Doesn't apply the contribution-specific
    extractor."""
    # A row with a real payload dict — original path.
    doc = {
        "ts": "2026-05-24T10:00:00+00:00",
        "brain": "alpha",
        "action": "promotion",
        "posted_as": "executor",
        "payload": {"symbol": "AAPL", "side": "long", "confidence": 0.8},
    }
    s = _normalize_sovereign(doc)["summary"]
    assert "AAPL" in s
    assert "long" in s


def test_back_compat_payload_dict_still_supported():
    """Some very old contribution rows put data in `payload`. The
    renderer must still read those rather than rendering them as
    skeleton."""
    doc = {
        "ts": "2026-05-15T10:00:00+00:00",
        "brain": "redeye",
        "action": "contribution",
        "posted_as": "opponent",
        "payload": {"symbol": "SOL/USD", "side": "SELL", "confidence": 0.6},
    }
    s = _normalize_sovereign(doc)["summary"]
    assert "SOL/USD" in s
    assert "SELL" in s
    assert "0.60" in s
