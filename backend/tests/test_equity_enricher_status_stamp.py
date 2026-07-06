"""Regression: `enrich_equity_doctrine_snapshot` must stamp
`enrichment_status` on the returned snapshot so downstream can
distinguish real data from cold-start defaults.

Contract:
    - Success path (`_enrich_sync` returns cleanly):
        enrichment_status = "live"
        enrichment_unavailable_fields = ["has_news", "float_millions"]
    - Exception path:
        enrichment_status = "failed"
        enrichment_error = repr(exc)
        Original base_snapshot fields preserved.
    - Empty symbol:
        enrichment_status = "no_symbol"
"""
from __future__ import annotations

import pytest

from shared.snapshot_enrich import equity_doctrine


@pytest.mark.asyncio
async def test_enricher_stamps_no_symbol_when_symbol_empty():
    base = {"symbol": "", "lane": "equity", "some_pre_field": 1}
    out = await equity_doctrine.enrich_equity_doctrine_snapshot("", base)
    assert out["enrichment_status"] == "no_symbol"
    assert out["some_pre_field"] == 1  # base preserved


@pytest.mark.asyncio
async def test_enricher_stamps_failed_on_exception(monkeypatch):
    """When the underlying Webull sync path throws, the async wrapper
    MUST stamp status=failed so the doctrine layer short-circuits to
    NO_DATA instead of silently returning to a manufactured REJECT."""

    def _boom(sym, snap):
        raise RuntimeError("webull creds not persisted")

    monkeypatch.setattr(equity_doctrine, "_enrich_sync", _boom)

    base = {"symbol": "AMH", "lane": "equity", "price": 12.3}
    out = await equity_doctrine.enrich_equity_doctrine_snapshot("AMH", base)

    assert out["enrichment_status"] == "failed"
    assert "webull creds not persisted" in out["enrichment_error"]
    # Pre-existing base fields must survive the failure.
    assert out["symbol"] == "AMH"
    assert out["price"] == 12.3


@pytest.mark.asyncio
async def test_enricher_stamps_live_and_unavailable_fields_on_success(monkeypatch):
    """Success path stamps status=live AND advertises the
    Webull-unavailable fields so `base_labels.py` can treat their
    absence as informational rather than adverse."""

    def _fake_success(sym, snap):
        # Simulate a successful enrichment that populated the fields
        # Webull DOES supply. Then let the real code append the
        # enrichment_status stamps via `_enrich_sync`'s tail — but
        # since we're replacing the whole sync function here, mimic
        # the same tail contract.
        return {
            **snap,
            "gap_pct": 12.0,
            "relative_volume": 5.4,
            "spread_bps": 30.0,
            "webull_enriched": True,
            "real_market_data": True,
            "primary_source": "webull",
            "data_council": ["webull"],
            "enrichment_status": "live",
            "enrichment_unavailable_fields": ["has_news", "float_millions"],
        }

    monkeypatch.setattr(equity_doctrine, "_enrich_sync", _fake_success)

    base = {"symbol": "NVDA", "lane": "equity"}
    out = await equity_doctrine.enrich_equity_doctrine_snapshot("NVDA", base)

    assert out["enrichment_status"] == "live"
    assert set(out["enrichment_unavailable_fields"]) == {"has_news", "float_millions"}
    assert out["gap_pct"] == 12.0
