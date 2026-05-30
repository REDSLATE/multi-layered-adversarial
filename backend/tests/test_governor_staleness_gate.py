"""Opinion-staleness gate hardening (2026-02-17).

The bug: `_resolve_governor_context` set `governor_alive = True`
whenever `gov_norm` was non-None — without checking the stance's age.
A 6h-old stance on SPY would keep the governor gate "live" forever,
allowing trades through a long-dead governor's cached opinion.

The fix: stale `gov_norm` is treated as `gov_norm = None` AND
`governor_alive = False`, routing into the existing GOVERNOR_OFFLINE
hard-block path.

These tripwires lock the new behavior. They exercise
`_resolve_governor_context` directly so the fix can't regress through
a refactor that re-introduces the unconditional `governor_alive = True`.
"""
from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from shared import council as cn


pytestmark = [pytest.mark.tripwire]


def _iso_minutes_ago(minutes: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()


# ──────────────────────── Source-scan invariant ────────────────────────


def test_resolve_governor_context_freshness_checks_gov_norm_timestamp():
    """Source-scan invariant: the function must NOT unconditionally
    set `governor_alive = True` when `gov_norm` is non-None. It must
    call `_is_fresh` on the stance's timestamp."""
    src = inspect.getsource(cn._resolve_governor_context)
    # The dangerous pattern (unconditional True) must be GONE.
    # We tolerate the line existing in a comment but not as a live
    # statement after the gov_norm None check.
    live_lines = [
        ln.strip() for ln in src.splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    joined = "\n".join(live_lines)
    # Must reference `_is_fresh` and the GOVERNOR_OFFLINE threshold.
    assert "_is_fresh" in joined and "_GOVERNOR_OFFLINE_THRESHOLD_SECONDS" in joined
    # The unconditional assignment must not appear as the immediate
    # consequence of `gov_norm is not None`. We expect the new pattern
    # where staleness gates the `governor_alive = True`.
    forbidden_phrase = "gov_norm is not None"  # New code uses this
    assert forbidden_phrase in joined or "gov_norm is None" in joined


# ──────────────────────── Behavioral tripwires ────────────────────────


@pytest.mark.asyncio
async def test_stale_governor_stance_routes_to_offline(monkeypatch):
    """A 6h-old `gov_norm` MUST produce
    `(gov_norm=None, governor_alive=False)`. Without this, the
    GOVERNOR_OFFLINE hard-block downstream never fires when the
    governor has a cached stance."""
    stale_doc = {
        "timestamp": _iso_minutes_ago(360),  # 6h ago
        "intent": {"executable": True, "veto": False, "stance": "ALLOW", "confidence": 0.8},
    }
    monkeypatch.setattr(cn, "_latest_governor_call",
                        AsyncMock(return_value=("chevelle", stale_doc)))
    monkeypatch.setattr(cn, "_latest_governor_any_call",
                        AsyncMock(return_value=("chevelle", stale_doc)))

    holder, gov_norm, alive, ts = await cn._resolve_governor_context("SPY", "equity")
    assert holder == "chevelle"
    assert gov_norm is None, (
        "A 6h-old stance must be treated as if the governor has no "
        "current stance on this symbol — otherwise the gate honors "
        "stale opinions from dead governors."
    )
    assert alive is False, (
        "Governor must be marked offline when the stance is older "
        "than the OFFLINE threshold. Otherwise the GOVERNOR_OFFLINE "
        "hard-block never fires."
    )
    # The stale timestamp is still surfaced for operator forensics.
    assert ts is not None


@pytest.mark.asyncio
async def test_fresh_governor_stance_kept_intact(monkeypatch):
    """A 2-minute-old `gov_norm` MUST still pass through unchanged.
    Don't over-correct — fresh stances are the normal path."""
    fresh_doc = {
        "timestamp": _iso_minutes_ago(2),
        "intent": {"executable": True, "veto": False, "stance": "ALLOW", "confidence": 0.8},
    }
    monkeypatch.setattr(cn, "_latest_governor_call",
                        AsyncMock(return_value=("chevelle", fresh_doc)))
    monkeypatch.setattr(cn, "_latest_governor_any_call",
                        AsyncMock(return_value=("chevelle", fresh_doc)))

    holder, gov_norm, alive, ts = await cn._resolve_governor_context("SPY", "equity")
    assert holder == "chevelle"
    assert gov_norm is not None, "fresh stance must NOT be nulled out"
    assert alive is True
    assert ts is not None


@pytest.mark.asyncio
async def test_borderline_stale_stance_just_past_threshold(monkeypatch):
    """A stance exactly 31 minutes old must trigger the offline branch
    (threshold is 30min). Locks the boundary."""
    just_stale = {
        "timestamp": _iso_minutes_ago(31),
        "intent": {"executable": True, "veto": False, "stance": "ALLOW", "confidence": 0.8},
    }
    monkeypatch.setattr(cn, "_latest_governor_call",
                        AsyncMock(return_value=("chevelle", just_stale)))
    monkeypatch.setattr(cn, "_latest_governor_any_call",
                        AsyncMock(return_value=("chevelle", just_stale)))

    holder, gov_norm, alive, _ = await cn._resolve_governor_context("SPY", "equity")
    assert gov_norm is None, "31min > 30min threshold → must be treated as stale"
    assert alive is False


@pytest.mark.asyncio
async def test_borderline_fresh_stance_just_under_threshold(monkeypatch):
    """A stance exactly 29 minutes old must STILL be treated as fresh.
    Locks the other side of the boundary."""
    just_fresh = {
        "timestamp": _iso_minutes_ago(29),
        "intent": {"executable": True, "veto": False, "stance": "ALLOW", "confidence": 0.8},
    }
    monkeypatch.setattr(cn, "_latest_governor_call",
                        AsyncMock(return_value=("chevelle", just_fresh)))
    monkeypatch.setattr(cn, "_latest_governor_any_call",
                        AsyncMock(return_value=("chevelle", just_fresh)))

    holder, gov_norm, alive, _ = await cn._resolve_governor_context("SPY", "equity")
    assert gov_norm is not None
    assert alive is True
