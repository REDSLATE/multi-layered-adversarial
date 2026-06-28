"""Regression tests for the consensus_boost_applied_rate KPI added to
Brain Metrics (operator-pinned 2026-06-24).

Operator's health bands:
  0–5%   → noise
  5–25%  → healthy selective influence
  25–50% → heavy
  50%+   → over_dependent

The metric reads `intent_consensus_telemetry` (the sidecar written
by seat_policy on every executor seat-floor evaluation). TTL on that
collection was bumped from 15min to 7d to support the full window.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest

from db import db, ensure_indexes
from namespaces import INTENT_CONSENSUS_TELEMETRY
from shared.brain_metrics import (
    APPLIED_RATE_HEALTH_BANDS,
    _classify_applied_rate,
    consensus_boost_applied_rate,
)


pytestmark = pytest.mark.asyncio


@pytest.fixture
async def clean_telemetry():
    """Drop + re-ensure indexes so the 7d TTL is in place."""
    await db[INTENT_CONSENSUS_TELEMETRY].drop()
    await ensure_indexes()
    yield db
    await db[INTENT_CONSENSUS_TELEMETRY].drop()


async def _seed_telemetry_row(
    intent_id: str,
    advisor_boost: float,
    minutes_ago: int = 0,
):
    """Insert a telemetry row at (now - minutes_ago)."""
    await db[INTENT_CONSENSUS_TELEMETRY].insert_one({
        "intent_id": intent_id,
        "ts": datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
        "applied": advisor_boost != 0.0,
        "base_confidence": 0.70,
        "advisor_boost": advisor_boost,
        "effective_confidence": max(0.0, min(1.0, 0.70 + advisor_boost)),
        "advisor_votes_used": 1 if advisor_boost != 0.0 else 0,
        "advisor_window_seconds": 900,
        "agree_count": 1 if advisor_boost > 0 else 0,
        "disagree_count": 1 if advisor_boost < 0 else 0,
        "agree_brains": [],
        "disagree_brains": [],
        "advisor_count": 1 if advisor_boost != 0.0 else 0,
    })


# ── Indexes ────────────────────────────────────────────────────────
async def test_telemetry_ttl_is_seven_days(clean_telemetry):
    """The TTL was bumped from 15min to 7d so the operator can view
    consensus_boost_applied_rate across the full window range."""
    idx = await db[INTENT_CONSENSUS_TELEMETRY].index_information()
    # The new index name is `consensus_telemetry_ttl_7d`.
    assert "consensus_telemetry_ttl_7d" in idx, (
        f"7d TTL index missing. indexes={list(idx.keys())}"
    )
    spec = idx["consensus_telemetry_ttl_7d"]
    assert spec["key"] == [("ts", 1)]
    assert spec.get("expireAfterSeconds") == 604800, (
        f"expected 7-day TTL (604800s), got {spec.get('expireAfterSeconds')}"
    )
    # The legacy 15min index MUST be gone (idempotent drop on boot).
    assert "consensus_telemetry_ttl_15m" not in idx, (
        "legacy 15min TTL index still present — drop logic broke"
    )


# ── Math / classification ──────────────────────────────────────────
# Tests pass `total=100` (above INSUFFICIENT_SAMPLES_THRESHOLD=50) so
# the band math is exercised. Below the threshold the classifier
# short-circuits to `insufficient_data` regardless of rate (operator
# pin 2026-02-22, observation-phase doctrine).
class TestClassifier:
    def test_no_data(self):
        assert _classify_applied_rate(None) == "no_data"

    def test_noise_band(self):
        assert _classify_applied_rate(0.0, total=100) == "noise"
        assert _classify_applied_rate(0.03, total=100) == "noise"
        assert _classify_applied_rate(0.049, total=100) == "noise"

    def test_healthy_band(self):
        assert _classify_applied_rate(0.05, total=100) == "healthy"
        assert _classify_applied_rate(0.15, total=100) == "healthy"
        assert _classify_applied_rate(0.249, total=100) == "healthy"

    def test_heavy_band(self):
        assert _classify_applied_rate(0.25, total=100) == "heavy"
        assert _classify_applied_rate(0.40, total=100) == "heavy"
        assert _classify_applied_rate(0.499, total=100) == "heavy"

    def test_over_dependent_band(self):
        assert _classify_applied_rate(0.50, total=100) == "over_dependent"
        assert _classify_applied_rate(0.85, total=100) == "over_dependent"
        assert _classify_applied_rate(1.00, total=100) == "over_dependent"

    def test_bands_cover_full_range(self):
        # Defensive: every rate in [0,1] MUST land in some band (when
        # sample size is sufficient — small-N is observability-only).
        for r in (0.0, 0.05, 0.06, 0.25, 0.499, 0.50, 0.99, 1.0):
            assert _classify_applied_rate(r, total=100) != "no_data"

    def test_insufficient_data_doctrine(self):
        """Operator pin 2026-02-22: below 50 evaluations the metric is
        observability-only. Verifies the classifier respects the
        threshold and surfaces `insufficient_data` (or the suspicious
        variant when rate > 0.5)."""
        # rate present but sample too small → insufficient_data
        assert _classify_applied_rate(0.15, total=10) == "insufficient_data"
        # rate suspicious AND sample too small → insufficient_data_suspicious
        assert _classify_applied_rate(0.80, total=10) == "insufficient_data_suspicious"
        # At threshold (50), band math kicks in
        assert _classify_applied_rate(0.15, total=50) == "healthy"


# ── End-to-end via the public function ─────────────────────────────
class TestAppliedRate:
    async def test_no_telemetry_returns_none(self, clean_telemetry):
        out = await consensus_boost_applied_rate(db, window_hours=24)
        assert out["applied_rate"] is None
        assert out["health_band"] == "no_data"
        assert out["total_evaluated"] == 0
        assert out["applied_count"] == 0

    async def test_zero_applied(self, clean_telemetry):
        # 50 evaluations (at INSUFFICIENT_SAMPLES_THRESHOLD), zero
        # boost on any of them. Band math activates at total >= 50.
        for i in range(50):
            await _seed_telemetry_row(f"i{i}", 0.0, minutes_ago=i)
        out = await consensus_boost_applied_rate(db, window_hours=24)
        assert out["applied_rate"] == 0.0
        assert out["health_band"] == "noise"
        assert out["total_evaluated"] == 50
        assert out["applied_count"] == 0

    async def test_healthy_band_15_percent(self, clean_telemetry):
        # 15 / 100 → 0.15 → healthy
        for i in range(15):
            await _seed_telemetry_row(f"a{i}", 0.05, minutes_ago=i)
        for i in range(85):
            await _seed_telemetry_row(f"z{i}", 0.0, minutes_ago=i)
        out = await consensus_boost_applied_rate(db, window_hours=24)
        assert out["applied_rate"] == 0.15
        assert out["health_band"] == "healthy"
        assert out["total_evaluated"] == 100
        assert out["applied_count"] == 15
        assert out["positive_boost_count"] == 15
        assert out["negative_boost_count"] == 0

    async def test_over_dependent_band(self, clean_telemetry):
        # 30 applied, 20 not → 60% → over_dependent (sample = 50 at
        # the INSUFFICIENT_SAMPLES_THRESHOLD so band math activates).
        for i in range(30):
            await _seed_telemetry_row(f"a{i}", 0.10, minutes_ago=i)
        for i in range(20):
            await _seed_telemetry_row(f"z{i}", 0.0, minutes_ago=i)
        out = await consensus_boost_applied_rate(db, window_hours=24)
        assert out["applied_rate"] == 0.6
        assert out["health_band"] == "over_dependent"

    async def test_window_excludes_old_rows(self, clean_telemetry):
        # 5 recent applied + 5 old (8 hours ago) applied.
        for i in range(5):
            await _seed_telemetry_row(f"recent{i}", 0.10, minutes_ago=i)
        for i in range(5):
            await _seed_telemetry_row(f"old{i}", 0.10, minutes_ago=8 * 60)
        # 1-hour window should only see the 5 recent ones.
        out = await consensus_boost_applied_rate(db, window_hours=1)
        assert out["total_evaluated"] == 5
        assert out["applied_count"] == 5
        # 24h window should see all 10.
        out = await consensus_boost_applied_rate(db, window_hours=24)
        assert out["total_evaluated"] == 10
        assert out["applied_count"] == 10

    async def test_mixed_positive_and_negative_boost(self, clean_telemetry):
        # 15 positive, 10 negative, 25 zero. applied = 25/50 = 50%.
        # Sample = 50 at the INSUFFICIENT_SAMPLES_THRESHOLD so band
        # math activates.
        for i in range(15):
            await _seed_telemetry_row(f"p{i}", 0.10, minutes_ago=i)
        for i in range(10):
            await _seed_telemetry_row(f"n{i}", -0.05, minutes_ago=i)
        for i in range(25):
            await _seed_telemetry_row(f"z{i}", 0.0, minutes_ago=i)
        out = await consensus_boost_applied_rate(db, window_hours=24)
        assert out["applied_rate"] == 0.5
        assert out["health_band"] == "over_dependent"
        assert out["positive_boost_count"] == 15
        assert out["negative_boost_count"] == 10

    async def test_applied_inferred_from_boost_value_when_flag_missing(
        self, clean_telemetry
    ):
        """Defensive: a row missing the `applied` flag (e.g. legacy
        row from before this metric existed) should still get counted
        as applied when `advisor_boost != 0`."""
        await db[INTENT_CONSENSUS_TELEMETRY].insert_one({
            "intent_id": "legacy",
            "ts": datetime.now(timezone.utc),
            # NOTE: no `applied` field.
            "advisor_boost": 0.10,
        })
        out = await consensus_boost_applied_rate(db, window_hours=24)
        assert out["applied_count"] == 1
        assert out["total_evaluated"] == 1


# ── Constants are exposed for the UI ────────────────────────────────
def test_health_band_constants_match_operator_spec():
    """The operator pinned exact band boundaries — pin them in code
    so a future refactor can't drift them silently."""
    labels = [label for label, _, _ in APPLIED_RATE_HEALTH_BANDS]
    assert labels == ["noise", "healthy", "heavy", "over_dependent"]
    # Edges (lo, hi) — order matters: 0-5%, 5-25%, 25-50%, 50%+
    edges = [(lo, hi) for _, lo, hi in APPLIED_RATE_HEALTH_BANDS]
    assert edges == [
        (0.0, 0.05),
        (0.05, 0.25),
        (0.25, 0.50),
        (0.50, 1.01),
    ]
