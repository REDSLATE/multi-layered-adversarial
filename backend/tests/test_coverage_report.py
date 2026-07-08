"""Unit tests for `shared/coverage_report.py`.

Focus is the pure logic — universe resolution + coverage aggregation.
DB-facing helpers (`_source_health`, `_all_snapshot_symbols`) are
tested through the public entry via a mocked db namespace.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared import coverage_report as cr


# ─── coverage math (pure) ────────────────────────────────────────


class TestLatestSnapshotBySymbol:
    def test_picks_newest_computed_at_per_symbol(self):
        docs = [
            {"symbol": "NVDA", "computed_at": "2026-01-01T10:00", "indicators": {"gap_pct": 1.0}},
            {"symbol": "NVDA", "computed_at": "2026-01-01T12:00", "indicators": {"gap_pct": 2.0}},
            {"symbol": "AAPL", "computed_at": "2026-01-01T09:00", "indicators": {"gap_pct": 3.0}},
        ]
        out = cr._latest_snapshot_by_symbol(docs)
        assert out["NVDA"]["indicators"]["gap_pct"] == 2.0  # newer wins
        assert out["AAPL"]["indicators"]["gap_pct"] == 3.0

    def test_missing_symbol_skipped(self):
        docs = [{"computed_at": "2026-01-01", "indicators": {}}]
        out = cr._latest_snapshot_by_symbol(docs)
        assert out == {}

    def test_missing_computed_at_still_kept(self):
        # Newer doc with no computed_at should still be treated
        # sanely — string comparison against "" makes it lose vs
        # any real ISO timestamp, so an older doc with a ts wins.
        docs = [
            {"symbol": "NVDA", "computed_at": "2026-01-01", "indicators": {"gap_pct": 1.0}},
            {"symbol": "NVDA", "indicators": {"gap_pct": 2.0}},  # no computed_at
        ]
        out = cr._latest_snapshot_by_symbol(docs)
        # The one with a real timestamp wins over the timestamp-less one.
        assert out["NVDA"]["indicators"]["gap_pct"] == 1.0


class TestCoverageForScope:
    @pytest.mark.asyncio
    async def test_empty_symbols_returns_empty(self):
        items, resolved = await cr._coverage_for_scope([])
        assert items == []
        assert resolved == 0

    @pytest.mark.asyncio
    async def test_field_populated_counts(self):
        # Three symbols; NVDA has gap+rvol; AAPL has gap only; MSFT has neither.
        fake_docs = [
            {"symbol": "NVDA", "computed_at": "2026-01-01T10:00",
             "indicators": {"gap_pct": 1.0, "relative_volume": 2.0}},
            {"symbol": "AAPL", "computed_at": "2026-01-01T10:00",
             "indicators": {"gap_pct": 3.0}},
            # MSFT: no snapshot at all
        ]

        class FakeColl:
            def find(self, query, projection):
                return FakeCursor()

        class FakeCursor:
            async def to_list(self, length=None):
                return fake_docs

        with patch.object(cr, "db", new={"shared_indicator_snapshots": FakeColl()}):
            items, resolved = await cr._coverage_for_scope(["NVDA", "AAPL", "MSFT"])

        by_field = {c.field: c for c in items}
        # gap_pct: NVDA + AAPL populated, MSFT missing → 2/3
        assert by_field["gap_pct"].populated == 2
        assert by_field["gap_pct"].total == 3
        assert "MSFT" in by_field["gap_pct"].missing_symbols
        # relative_volume: only NVDA populated → 1/3
        assert by_field["relative_volume"].populated == 1
        assert "AAPL" in by_field["relative_volume"].missing_symbols
        assert "MSFT" in by_field["relative_volume"].missing_symbols
        # market_regime: not populated for anyone (Follow-up A pending) → 0/3
        assert by_field["market_regime"].populated == 0
        assert by_field["market_regime"].pct == 0.0
        # snapshots_resolved counts symbols WITH a snapshot, not all in universe
        assert resolved == 2

    @pytest.mark.asyncio
    async def test_nan_treated_as_missing(self):
        # A float NaN must count as missing, not populated — the
        # doctrine layer would misread NaN as valid.
        nan = float("nan")
        fake_docs = [
            {"symbol": "NVDA", "computed_at": "2026-01-01",
             "indicators": {"gap_pct": nan, "relative_volume": 2.0}},
        ]

        class FakeColl:
            def find(self, query, projection):
                class C:
                    async def to_list(self, length=None):
                        return fake_docs
                return C()

        with patch.object(cr, "db", new={"shared_indicator_snapshots": FakeColl()}):
            items, _ = await cr._coverage_for_scope(["NVDA"])
        by_field = {c.field: c for c in items}
        assert by_field["gap_pct"].populated == 0  # NaN counts as missing
        assert "NVDA" in by_field["gap_pct"].missing_symbols
        assert by_field["relative_volume"].populated == 1

    @pytest.mark.asyncio
    async def test_missing_samples_capped(self):
        # 50 symbols, all missing. Missing sample list must cap at
        # MISSING_SAMPLE_CAP (20) to keep the response bounded.
        symbols = [f"SYM{i:03d}" for i in range(50)]

        class FakeColl:
            def find(self, query, projection):
                class C:
                    async def to_list(self, length=None):
                        return []
                return C()

        with patch.object(cr, "db", new={"shared_indicator_snapshots": FakeColl()}):
            items, _ = await cr._coverage_for_scope(symbols)
        by_field = {c.field: c for c in items}
        # All 50 missing but only 20 in the sample list.
        assert by_field["gap_pct"].populated == 0
        assert by_field["gap_pct"].total == 50
        assert len(by_field["gap_pct"].missing_symbols) == cr.MISSING_SAMPLE_CAP


class TestSerializeCoverage:
    def test_groups_fields_by_category(self):
        items = [
            cr.FieldCoverage(field="gap_pct", populated=10, total=10,
                             pct=100.0, missing_symbols=[]),
            cr.FieldCoverage(field="market_regime", populated=0, total=10,
                             pct=0.0, missing_symbols=[]),
            cr.FieldCoverage(field="atr14", populated=9, total=10,
                             pct=90.0, missing_symbols=["MSFT"]),
        ]
        out = cr._serialize_coverage(items)
        # gap_pct falls into session_features_v1
        assert "gap_pct" in out["session_features_v1"]
        assert out["session_features_v1"]["gap_pct"]["pct"] == 100.0
        # market_regime shipped 2026-02-20 → now in session_features_v2
        assert out["session_features_v2"]["market_regime"]["pct"] == 0.0
        # atr14 under legacy_indicators with the missing sample carried
        assert out["legacy_indicators"]["atr14"]["missing_symbols"] == ["MSFT"]


class TestBuildCoverageReportRejectsBadScope:
    @pytest.mark.asyncio
    async def test_unknown_scope_raises(self):
        with pytest.raises(ValueError, match="unknown scope"):
            await cr.build_coverage_report("garbage")


# ─── source-health status classification ─────────────────────────


class TestSourceHealthStatus:
    """The `status` field is derived in the build_coverage_report
    serializer, not by _source_health() itself. Test the boundary
    conditions via a live build with mocked source data.
    """

    @pytest.mark.asyncio
    async def test_healthy_recent_success_reads_ok(self):
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)

        async def _no_symbols():
            return []

        # Mock the source-health function to return one healthy source.
        async def fake_health():
            return [
                cr.SourceHealth(
                    source="polygon_flatfiles",
                    last_success_ts=now.isoformat(),
                    last_error_ts=None,
                    last_error_message=None,
                    minutes_since_last_success=5.0,
                ),
            ]

        with patch.object(cr, "_live_universe_symbols", new=_no_symbols), \
             patch.object(cr, "_source_health", new=fake_health):
            report = await cr.build_coverage_report("live_universe")

        assert report["per_source_health"][0]["source"] == "polygon_flatfiles"
        assert report["per_source_health"][0]["status"] == "ok"

    @pytest.mark.asyncio
    async def test_stale_success_reads_stale(self):
        # A source that succeeded 3 hours ago (> 120 min threshold) is stale.
        async def fake_health():
            return [
                cr.SourceHealth(
                    source="polygon_news_witness",
                    last_success_ts="2026-01-01T00:00:00+00:00",
                    last_error_ts=None,
                    last_error_message=None,
                    minutes_since_last_success=180.0,
                ),
            ]

        async def _no_symbols():
            return []

        with patch.object(cr, "_live_universe_symbols", new=_no_symbols), \
             patch.object(cr, "_source_health", new=fake_health):
            report = await cr.build_coverage_report("live_universe")
        assert report["per_source_health"][0]["status"] == "stale"

    @pytest.mark.asyncio
    async def test_no_success_ever_reads_no_data(self):
        async def fake_health():
            return [
                cr.SourceHealth(
                    source="kraken_pro",
                    last_success_ts=None,
                    last_error_ts=None,
                    last_error_message=None,
                    minutes_since_last_success=None,
                ),
            ]

        async def _no_symbols():
            return []

        with patch.object(cr, "_live_universe_symbols", new=_no_symbols), \
             patch.object(cr, "_source_health", new=fake_health):
            report = await cr.build_coverage_report("live_universe")
        assert report["per_source_health"][0]["status"] == "no_data"
