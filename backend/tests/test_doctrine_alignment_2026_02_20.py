"""Tests for the 2026-02-20 doctrine alignment patches.

Operator doctrine (pinned here):

    Brain      = opinion only
    Seat       = restriction authority
    Governor   = modifier
    RoadGuard  = hard stop

This suite locks the three doctrine-aligning patches that shipped
2026-02-20:

  1. `matches_tier_1` normalizes brain names across legacy stack
     codes, canonical brain_ids, and UI display names.
  2. `TIER_1_DEFAULTS.confidence_min = 0.70` (was 0.85).
  3. `TIER_1_DEFAULTS.notional_default_usd = 10.0` (was 5.0).
"""
from __future__ import annotations

import pytest

from shared.auto_submit_policy import (
    TIER_1_DEFAULTS,
    _normalize_brain_to_stack,
    chosen_notional,
    matches_tier_1,
    reset_policy_for_tests,
    set_policy,
)


@pytest.fixture(autouse=True)
def _bypass_market_hours(monkeypatch):
    """All tests in this file are about the policy layer, not the
    market-hours gate. Force bypass so the suite passes regardless of
    wall-clock time."""
    monkeypatch.setenv("RISEDUAL_BYPASS_MARKET_HOURS", "true")
    reset_policy_for_tests()
    yield
    reset_policy_for_tests()


# ── Loosened tier-1 defaults ─────────────────────────────────────


def test_confidence_min_default_is_0_70():
    """Operator's preferred default. 0.85 was suppressing actionable
    signals; 0.70 matches what the operator has been hand-flipping
    to on every prod deploy."""
    assert TIER_1_DEFAULTS["confidence_min"] == 0.70


def test_notional_default_usd_is_10():
    """Dynamic Webull cap is BP-scaled; $10 fits inside the ceiling
    of any funded account."""
    assert TIER_1_DEFAULTS["notional_default_usd"] == 10.0


def test_chosen_notional_respects_loosened_default():
    """When the brain doesn't specify `preferred_notional_usd`,
    `chosen_notional` uses the loosened default. With no per-order
    cap supplied, the default is the minimum of (default, max) → 10."""
    set_policy(True)
    intent = {"action": "BUY", "lane": "equity", "stack": "alpha", "confidence": 0.9}
    assert chosen_notional(intent) == 10.0


# ── Brain name normalization ─────────────────────────────────────


@pytest.mark.parametrize("variant,expected", [
    # legacy stack codes — pass through unchanged
    ("alpha", "alpha"),
    ("camaro", "camaro"),
    ("chevelle", "chevelle"),
    ("redeye", "redeye"),
    # canonical brain_ids — resolve via BRAIN_ID_TO_STACK
    ("camino", "alpha"),
    ("barracuda", "camaro"),
    ("hellcat", "chevelle"),
    ("gto", "redeye"),
    # UI display names (case-insensitive) — also resolve
    ("Camino", "alpha"),
    ("Barracuda", "camaro"),
    ("Hellcat", "chevelle"),
    ("GTO", "redeye"),
    # whitespace tolerance
    ("  camaro  ", "camaro"),
    ("  Hellcat  ", "chevelle"),
])
def test_normalize_brain_handles_all_three_forms(variant, expected):
    assert _normalize_brain_to_stack(variant) == expected


def test_normalize_brain_passes_through_unknown_unchanged():
    """Unknown identifiers are lowercased and returned as-is so the
    audit reason carries the original token for diagnosis."""
    assert _normalize_brain_to_stack("INVALID_BRAIN") == "invalid_brain"
    assert _normalize_brain_to_stack("") == ""


def test_matches_tier_1_accepts_display_name():
    """Operator's pain: a brain emitting with `stack="Hellcat"` was
    silently filtered because the allowed list is keyed on `chevelle`.
    Normalization fixes this."""
    set_policy(True)
    intent = {
        "action": "BUY", "lane": "equity", "stack": "Hellcat",
        "confidence": 0.95, "dry_run_state": "passed",
    }
    ok, reason = matches_tier_1(intent)
    assert ok is True, f"display-name 'Hellcat' must normalize → 'chevelle': {reason}"


def test_matches_tier_1_accepts_brain_id():
    set_policy(True)
    intent = {
        "action": "BUY", "lane": "crypto", "stack": "barracuda",
        "confidence": 0.95, "dry_run_state": "passed",
    }
    ok, reason = matches_tier_1(intent)
    assert ok is True, f"brain_id 'barracuda' must normalize → 'camaro': {reason}"


def test_matches_tier_1_unknown_brain_still_rejected():
    """Normalization is permissive only for known brains. An unknown
    name still filters out — and the audit reason MUST surface both
    the raw and normalized form so the operator can debug."""
    set_policy(True)
    intent = {
        "action": "BUY", "lane": "equity", "stack": "GHOSTBRAIN",
        "confidence": 0.95, "dry_run_state": "passed",
    }
    ok, reason = matches_tier_1(intent)
    assert ok is False
    assert "ghostbrain" in reason.lower()
    assert "normalized" in reason.lower()


def test_matches_tier_1_loosened_confidence_pass():
    """0.70 intent should now pass under the new default (would have
    failed pre-2026-02-20 when default was 0.85)."""
    set_policy(True)
    intent = {
        "action": "BUY", "lane": "equity", "stack": "alpha",
        "confidence": 0.70, "dry_run_state": "passed",
    }
    ok, reason = matches_tier_1(intent)
    assert ok is True, reason


def test_matches_tier_1_below_loosened_floor_still_rejected():
    """0.65 still below the loosened 0.70 floor — must reject."""
    set_policy(True)
    intent = {
        "action": "BUY", "lane": "equity", "stack": "alpha",
        "confidence": 0.65, "dry_run_state": "passed",
    }
    ok, reason = matches_tier_1(intent)
    assert ok is False
    assert "0.65" in reason or "0.650" in reason
