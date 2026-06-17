"""Tests for the auto_submit_skipped audit + categorization (2026-02-19).

When Shelly evaluates an intent and decides not to submit (HOLD signal,
low-conf, wrong lane, etc.), she now writes an `auto_submit_skipped`
row to `shared_gate_results` with a `skip_category`. The post-mortem
classifier reads those rows and surfaces them as their own outcome
bucket so the operator can distinguish "Shelly correctly filtered
this" from "pipeline silently stuck".
"""
from __future__ import annotations

import pytest

from shared.auto_submit_policy import (
    SKIP_CATEGORY_ACTION_FILTERED,
    SKIP_CATEGORY_ALREADY_EXECUTED,
    SKIP_CATEGORY_BRAIN_FILTERED,
    SKIP_CATEGORY_DISABLED,
    SKIP_CATEGORY_DRY_RUN_NOT_READY,
    SKIP_CATEGORY_HOLD,
    SKIP_CATEGORY_LANE_FILTERED,
    SKIP_CATEGORY_LOW_CONFIDENCE,
    SKIP_CATEGORY_OTHER,
    _categorize_skip,
    matches_tier_1,
)


@pytest.fixture(autouse=True)
def _bypass_market_hours(monkeypatch):
    """These tests are about skip categorization, not the market-hours
    gate (which has its own dedicated test suite). Bypass so the suite
    passes regardless of wall-clock time."""
    monkeypatch.setenv("RISEDUAL_BYPASS_MARKET_HOURS", "true")


def test_categorize_hold():
    """HOLD is the dominant case — must surface separately so the
    operator sees 'Shelly correctly skipped 3500 HOLD signals' at a
    glance instead of getting lost in the action_filtered bucket."""
    ok, reason = matches_tier_1(
        {"action": "HOLD", "lane": "equity", "stack": "alpha", "confidence": 0.9, "dry_run_state": "passed"},
        {"enabled": True, "allowed_actions": ["BUY", "SELL"], "allowed_lanes": ["equity", "crypto"],
         "allowed_brains": ["alpha"], "confidence_min": 0.85, "required_dry_run_state": "passed",
         "tier_name": "tier_1_conservative"},
    )
    assert ok is False
    assert _categorize_skip(reason) == SKIP_CATEGORY_HOLD


def test_categorize_low_confidence():
    ok, reason = matches_tier_1(
        {"action": "BUY", "lane": "equity", "stack": "alpha", "confidence": 0.5, "dry_run_state": "passed"},
        {"enabled": True, "allowed_actions": ["BUY", "SELL"], "allowed_lanes": ["equity", "crypto"],
         "allowed_brains": ["alpha"], "confidence_min": 0.85, "required_dry_run_state": "passed",
         "tier_name": "tier_1_conservative"},
    )
    assert ok is False
    assert _categorize_skip(reason) == SKIP_CATEGORY_LOW_CONFIDENCE


def test_categorize_lane_filtered():
    ok, reason = matches_tier_1(
        {"action": "BUY", "lane": "options", "stack": "alpha", "confidence": 0.9, "dry_run_state": "passed"},
        {"enabled": True, "allowed_actions": ["BUY", "SELL"], "allowed_lanes": ["equity", "crypto"],
         "allowed_brains": ["alpha"], "confidence_min": 0.85, "required_dry_run_state": "passed",
         "tier_name": "tier_1_conservative"},
    )
    assert ok is False
    assert _categorize_skip(reason) == SKIP_CATEGORY_LANE_FILTERED


def test_categorize_brain_filtered():
    ok, reason = matches_tier_1(
        {"action": "BUY", "lane": "equity", "stack": "rogue_brain", "confidence": 0.9, "dry_run_state": "passed"},
        {"enabled": True, "allowed_actions": ["BUY", "SELL"], "allowed_lanes": ["equity", "crypto"],
         "allowed_brains": ["alpha"], "confidence_min": 0.85, "required_dry_run_state": "passed",
         "tier_name": "tier_1_conservative"},
    )
    assert ok is False
    assert _categorize_skip(reason) == SKIP_CATEGORY_BRAIN_FILTERED


def test_categorize_dry_run_not_ready():
    ok, reason = matches_tier_1(
        {"action": "BUY", "lane": "equity", "stack": "alpha", "confidence": 0.9, "dry_run_state": "pending"},
        {"enabled": True, "allowed_actions": ["BUY", "SELL"], "allowed_lanes": ["equity", "crypto"],
         "allowed_brains": ["alpha"], "confidence_min": 0.85, "required_dry_run_state": "passed",
         "tier_name": "tier_1_conservative"},
    )
    assert ok is False
    assert _categorize_skip(reason) == SKIP_CATEGORY_DRY_RUN_NOT_READY


def test_categorize_disabled():
    ok, reason = matches_tier_1(
        {"action": "BUY", "lane": "equity", "stack": "alpha", "confidence": 0.9, "dry_run_state": "passed"},
        {"enabled": False, "allowed_actions": ["BUY", "SELL"], "allowed_lanes": ["equity"],
         "allowed_brains": ["alpha"], "confidence_min": 0.85, "required_dry_run_state": "passed",
         "tier_name": "tier_1_conservative"},
    )
    assert ok is False
    assert _categorize_skip(reason) == SKIP_CATEGORY_DISABLED


def test_categorize_already_executed():
    ok, reason = matches_tier_1(
        {"action": "BUY", "lane": "equity", "stack": "alpha", "confidence": 0.9,
         "dry_run_state": "passed", "executed": True},
        {"enabled": True, "allowed_actions": ["BUY", "SELL"], "allowed_lanes": ["equity"],
         "allowed_brains": ["alpha"], "confidence_min": 0.85, "required_dry_run_state": "passed",
         "tier_name": "tier_1_conservative"},
    )
    assert ok is False
    assert _categorize_skip(reason) == SKIP_CATEGORY_ALREADY_EXECUTED


def test_categorize_unknown_falls_back_to_other():
    assert _categorize_skip("this is a brand new reason string") == SKIP_CATEGORY_OTHER
    assert _categorize_skip("") == SKIP_CATEGORY_OTHER


def test_action_filtered_separate_from_hold():
    """A future non-HOLD/BUY/SELL action (e.g. COVER) should bucket as
    action_filtered, NOT as hold_action. HOLD gets its own bucket
    because it's the 99% case and the operator wants to see it
    distinctly."""
    assert _categorize_skip("action 'COVER' not in allowed ['BUY', 'SELL']") == SKIP_CATEGORY_ACTION_FILTERED
    assert _categorize_skip("action 'HOLD' not in allowed ['BUY', 'SELL']") == SKIP_CATEGORY_HOLD
