"""Crypto snapshot enrichment + NO_DATA doctrine distinction.

Operator doctrine: real unfavorable market data → REJECT; missing or
stale market data → NO_DATA. Sentinels (spread 9999, vol/volume 0)
must never be graded as market conditions.
"""
from __future__ import annotations

import sys

import pytest

sys.path.insert(0, "/app/backend")

from shared.crypto.doctrine.crypto_brain_sidecars import (
    build_crypto_brain_doctrine_packet,
)
from shared.crypto.doctrine.crypto_labels import label_crypto_snapshot
from shared.market_data import crypto_snapshot_enrichment as enr
from shared.observability import pipeline_counters


TICKER = {"bid": 118000.0, "ask": 118010.0, "volume_24h_usd": 850_000_000.0}


def _bars(n=12, base=118000.0, step=40.0):
    return [{"o": base + i * step, "h": base + i * step + 60,
             "l": base + i * step - 60, "c": base + (i + 1) * step,
             "ts": f"2026-07-27T{10 + i // 12:02d}:{(i * 5) % 60:02d}:00+00:00"}
            for i in range(n)]


@pytest.fixture
def wired(monkeypatch):
    enr.reset_for_tests()

    async def fake_ticker(symbol):
        return dict(TICKER)

    async def fake_bars(symbol):
        return 0.0032, 0.85, 12
    monkeypatch.setattr(enr, "_fetch_kraken_ticker", fake_ticker)
    monkeypatch.setattr(enr, "_bars_features", fake_bars)
    yield monkeypatch
    enr.reset_for_tests()


# ── enrichment fills missing fields; brain values win ────────────────

@pytest.mark.asyncio
async def test_enrichment_fills_all_required_fields(wired):
    out, diag = await enr.enrich_crypto_snapshot({}, symbol="BTC/USD")
    assert out["enrichment_status"] == "ENRICHED"
    assert out["missing_required_fields"] == []
    assert out["bid"] == 118000.0 and out["ask"] == 118010.0
    assert out["volume_24h_usd"] == 850_000_000.0
    assert 0 < out["spread_bps"] < 2       # ~0.85bps real spread
    assert out["volatility_1h"] == 0.0032
    assert out["trend_strength"] == 0.85
    assert out["snapshot_source"] == "KRAKEN_PUBLIC"
    assert out["snapshot_age_ms"] == 0.0
    assert out["bars_used"] == 12


@pytest.mark.asyncio
async def test_brain_values_win_over_mc(wired):
    out, _ = await enr.enrich_crypto_snapshot(
        {"bid": 117000.0, "ask": 117020.0, "spread_bps": 1.7,
         "volatility_1h": 0.01}, symbol="BTC/USD")
    assert out["bid"] == 117000.0
    assert out["spread_bps"] == 1.7
    assert out["spread_source"] == "BRAIN"
    assert out["volatility_1h"] == 0.01
    assert out["trend_strength"] == 0.85     # MC filled the gap


# ── fail-closed protections ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_no_ticker_no_cache_is_no_data(monkeypatch):
    enr.reset_for_tests()

    async def boom(symbol):
        raise RuntimeError("kraken down")

    async def fake_bars(symbol):
        return 0.003, 0.5, 12
    monkeypatch.setattr(enr, "_fetch_kraken_ticker", boom)
    monkeypatch.setattr(enr, "_bars_features", fake_bars)
    out, _ = await enr.enrich_crypto_snapshot({}, symbol="BTC/USD")
    assert out["enrichment_status"] == "NO_DATA"
    assert "bid" in out["missing_required_fields"]
    # RoadGuard compat: sentinel present but ONLY alongside NO_DATA
    assert out["spread_bps"] == 9999.0
    assert out["spread_source"] == "MC_SENTINEL"


@pytest.mark.asyncio
async def test_invalid_quotes_are_no_data(wired):
    out, _ = await enr.enrich_crypto_snapshot(
        {"bid": 118010.0, "ask": 118000.0, "volume_24h_usd": 1e9,
         "volatility_1h": 0.003, "trend_strength": 0.5, "spread_bps": 1.0},
        symbol="BTC/USD")
    assert out["enrichment_status"] == "NO_DATA"


@pytest.mark.asyncio
async def test_insufficient_bars_is_no_data(monkeypatch):
    enr.reset_for_tests()

    async def fake_ticker(symbol):
        return dict(TICKER)

    async def few_bars(symbol):
        return None, None, 3
    monkeypatch.setattr(enr, "_fetch_kraken_ticker", fake_ticker)
    monkeypatch.setattr(enr, "_bars_features", few_bars)
    out, _ = await enr.enrich_crypto_snapshot({}, symbol="NEW/USD")
    assert out["enrichment_status"] == "NO_DATA"
    assert "volatility_1h" in out["missing_required_fields"]


# ── bars math ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_bars_features_math(monkeypatch):
    class _Cur:
        def __init__(self, rows):
            self._rows = rows

        def sort(self, *a):
            return self

        def limit(self, n):
            return self

        def __aiter__(self):
            async def gen():
                for r in self._rows:
                    yield r
            return gen()

    rows = list(reversed(_bars(12)))   # db returns ts desc
    import shared.market_data.crypto_snapshot_enrichment as m

    class _Db:
        def __getitem__(self, name):
            return type("C", (), {"find": lambda s, *a, **k: _Cur(rows)})()
    monkeypatch.setitem(sys.modules, "db", type("M", (), {"db": _Db()}))
    vol, trend, used = await m._bars_features("BTC/USD")
    assert used == 12
    assert vol > 0
    assert trend == 1.0     # monotonic climb → perfectly efficient


# ── doctrine: NO_DATA vs REJECT distinction ──────────────────────────

def _graded(snapshot):
    snapshot.setdefault("symbol", "BTC/USD")
    snapshot.setdefault("lane", "crypto")
    return label_crypto_snapshot(snapshot)


def test_labeler_no_data_never_graded_as_market():
    base = _graded({"enrichment_status": "NO_DATA",
                    "missing_required_fields": ["bid", "ask"]})
    assert base.quality == "NO_DATA"
    assert base.labels == ["NO_DATA"]
    # the OLD failure mode must be gone: no WIDE_SPREAD / DEAD_VOL
    assert "WIDE_SPREAD" not in base.labels
    assert "DEAD_VOL" not in base.labels


def test_labeler_legacy_empty_snapshot_is_no_data_not_reject():
    # historical intents without enrichment stamps: sentinel spread +
    # zero quotes → NO_DATA, never REJECT
    base = _graded({})
    assert base.quality == "NO_DATA"


def test_labeler_real_good_data_grades_positively():
    base = _graded({
        "enrichment_status": "ENRICHED", "missing_required_fields": [],
        "bid": 118000.0, "ask": 118010.0, "spread_bps": 0.85,
        "volume_24h_usd": 850_000_000.0, "volatility_1h": 0.008,
        "trend_strength": 0.7,
    })
    assert base.quality in ("B_QUALITY", "A_QUALITY")
    assert base.score >= 0.60


def test_labeler_real_bad_data_still_rejects():
    # REAL wide spread + dead vol with valid quotes → genuine REJECT
    base = _graded({
        "enrichment_status": "ENRICHED", "missing_required_fields": [],
        "bid": 0.0010, "ask": 0.0012, "spread_bps": 1800.0,
        "volume_24h_usd": 40_000.0, "volatility_1h": 0.0004,
        "trend_strength": 0.05,
    })
    assert base.quality == "REJECT"
    assert "WIDE_SPREAD" in base.labels


# ── packet provenance + governor NO_DATA dampener ────────────────────

def test_packet_carries_provenance_and_no_data_dampener():
    packet = build_crypto_brain_doctrine_packet({
        "symbol": "BTC/USD", "lane": "crypto",
        "enrichment_status": "NO_DATA",
        "missing_required_fields": ["volume_24h_usd"],
        "snapshot_source": "NO_DATA", "spread_source": "MC_SENTINEL",
        "snapshot_age_ms": -1.0, "bars_used": 0,
    })
    prov = packet["market_data_provenance"]
    assert prov["enrichment_status"] == "NO_DATA"
    assert prov["doctrine_result"] == "NO_DATA"
    assert prov["doctrine_score"] == 0.0
    gov = packet["seats"]["governor"]
    names = [d[0] if isinstance(d, (list, tuple)) else d.get("name")
             for d in gov["dampeners"]]
    assert "NO_DATA_CONSERVATIVE" in str(names)
    assert gov["risk_multiplier"] <= 0.50    # never sizes UP on dark data


def test_packet_provenance_on_enriched():
    packet = build_crypto_brain_doctrine_packet({
        "symbol": "BTC/USD", "lane": "crypto",
        "enrichment_status": "ENRICHED", "missing_required_fields": [],
        "bid": 118000.0, "ask": 118010.0, "spread_bps": 0.85,
        "volume_24h_usd": 850_000_000.0, "volatility_1h": 0.008,
        "trend_strength": 0.7, "snapshot_source": "KRAKEN_PUBLIC",
        "spread_source": "MC_KRAKEN_PUBLIC", "snapshot_age_ms": 0.0,
        "bars_used": 12,
    })
    prov = packet["market_data_provenance"]
    assert prov["snapshot_source"] == "KRAKEN_PUBLIC"
    assert prov["bars_used"] == 12
    assert prov["doctrine_result"] in ("A_QUALITY", "B_QUALITY")


# ── counters ─────────────────────────────────────────────────────────

def test_pipeline_counters():
    pipeline_counters.reset_for_tests()
    pipeline_counters.incr("brains_evaluated", 4)
    pipeline_counters.incr("intents_no_data")
    snap = pipeline_counters.snapshot()
    assert snap["brains_evaluated"] == 4
    assert snap["intents_no_data"] == 1
    assert snap["intents_rejected"] == 0
    pipeline_counters.reset_for_tests()
