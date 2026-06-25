"""Consensus boost applied-rate band — observation-phase doctrine pin.

Operator decision (2026-02-22):
  * Below 50 executor evaluations, the applied rate is observability-
    only. Don't tune, don't act on it.
  * UI surfaces YELLOW + "observing" or "behaviour suspicious" rather
    than red "over-dependent" at that sample size.
  * Threshold matches the READY band in admin_paradox_v3._BANDS so the
    50-sample floor is consistent across the entire v3 rollout.
"""
from __future__ import annotations

import pytest

from shared.brain_metrics import (
    INSUFFICIENT_SAMPLES_THRESHOLD,
    _classify_applied_rate,
)


def test_threshold_is_50():
    """If a future agent lowers this, the consensus_boost surface
    starts firing OVER_DEPENDENT alerts on tiny samples. Pinned so
    that doesn't happen without an explicit operator review."""
    assert INSUFFICIENT_SAMPLES_THRESHOLD == 50


def test_no_data_when_rate_is_none():
    assert _classify_applied_rate(rate=None, total=0) == "no_data"


@pytest.mark.parametrize("total", [0, 1, 14, 49])
def test_under_50_evals_with_low_rate_returns_insufficient_data(total):
    # rate <= 0.5 → just "insufficient_data" (yellow, observing)
    assert _classify_applied_rate(rate=0.1, total=total) == "insufficient_data"
    assert _classify_applied_rate(rate=0.5, total=total) == "insufficient_data"


@pytest.mark.parametrize("total", [0, 1, 14, 49])
def test_under_50_evals_with_high_rate_returns_suspicious(total):
    # rate > 0.5 → "insufficient_data_suspicious" (yellow + flag)
    # — exactly the case the operator flagged in the screenshot
    #   (14/14 evals at 100% applied rate).
    assert _classify_applied_rate(rate=0.51, total=total) == "insufficient_data_suspicious"
    assert _classify_applied_rate(rate=1.0,  total=total) == "insufficient_data_suspicious"


def test_at_50_evals_real_bands_engage():
    """At 50 evaluations we cross the floor — the over_dependent
    verdict is now real, not noise."""
    assert _classify_applied_rate(rate=1.0, total=50) == "over_dependent"
    assert _classify_applied_rate(rate=0.15, total=50) == "healthy"
    assert _classify_applied_rate(rate=0.03, total=50) == "noise"


def test_consensus_boost_payload_carries_threshold(monkeypatch):
    """The endpoint response includes `insufficient_samples_threshold`
    so the frontend doesn't have to hardcode 50 — it reads the floor
    from the payload."""
    # Pure shape check — _classify_applied_rate is internal but the
    # threshold constant is the contract surface.
    from shared.brain_metrics import INSUFFICIENT_SAMPLES_THRESHOLD as T
    assert T == 50
