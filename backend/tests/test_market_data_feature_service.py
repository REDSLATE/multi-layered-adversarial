"""Feature-service doctrine tripwires (2026-02-17).

Locks the contract of `shared/market_data/feature_service.py`:

  1. `compute_relative_volume` returns `value=None` (not 0.0) when bars
     are insufficient. False 0.0 → false-positive STUCK_FEATURES self-
     veto downstream.
  2. `compute_relative_volume` returns `value=None` on zero baseline
     mean (no silent NaN from division by zero).
  3. `compute_relative_volume` returns a sensible float when bars are
     present and the baseline mean is positive.
  4. `fetch_has_news` returns `has_news=None, ok=False` on missing
     `FINNHUB_API_KEY` (not crash, not silent False).
  5. `fetch_has_news` returns `has_news=False` on a successful empty
     fetch. Distinct from None.
  6. `build_market_snapshot` separates "data not present" from
     "data present and zero" via dedicated `*_ok`/`*_reason` fields.
  7. News cache TTL is honored (second call within TTL hits cache).
  8. Routes registered + dual-auth helper rejects unknown principal.
"""
from __future__ import annotations

import asyncio
import inspect
import os
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from shared.market_data import feature_service as fs


pytestmark = [pytest.mark.tripwire]


# ──────────────────────── helpers ────────────────────────


def _bars(volumes: list[float], symbol: str = "TEST", tf: str = "5m"):
    """Build a list of bar docs sorted descending by ts (latest first),
    matching what the .find().sort('ts',-1) query returns."""
    out = []
    base_ts = 1_700_000_000
    for i, v in enumerate(volumes):
        out.append({
            "source": "finnhub_equity",
            "symbol": symbol,
            "tf": tf,
            "ts": f"2026-01-01T00:{i:02d}:00+00:00",
            "v": float(v),
        })
    return out


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def sort(self, *a, **kw):
        return self

    def limit(self, *a, **kw):
        return self

    async def to_list(self, length=None):
        return self._rows[: length or len(self._rows)]


class _FakeCollection:
    def __init__(self, rows):
        self._rows = rows

    def find(self, *a, **kw):
        return _FakeCursor(self._rows)


class _FakeDB:
    def __init__(self, rows):
        self._rows = rows

    def __getitem__(self, name):
        return _FakeCollection(self._rows)


# ──────────────────────── relative_volume tripwires ────────────────────────


@pytest.mark.asyncio
async def test_rvol_none_when_no_bars():
    """No bars → value MUST be None, NOT 0.0. 0.0 is a real signal
    (no volume right now); None is the only correct 'unknown' code."""
    db = _FakeDB([])
    result = await fs.compute_relative_volume("NVDA", db=db)
    assert result["value"] is None
    assert result["ok"] is False
    assert result["reason"] == "no_bars_for_symbol"
    assert result["basis_bars"] == 0


@pytest.mark.asyncio
async def test_rvol_none_when_basis_too_small():
    """Below MIN_BASIS_BARS baseline bars → None. Prevents noisy RVOL
    from a 1- or 2-bar window dominating the labeler."""
    # 1 current + 3 baseline = 3 < MIN_BASIS_BARS(5)
    db = _FakeDB(_bars([1000, 500, 500, 500]))
    result = await fs.compute_relative_volume("NVDA", db=db)
    assert result["value"] is None, result
    assert result["ok"] is False
    assert "basis_bars_below_minimum" in result["reason"]
    assert result["basis_bars"] == 3


@pytest.mark.asyncio
async def test_rvol_none_when_baseline_mean_zero():
    """Baseline all-zero volume → cannot divide; None not NaN/inf.
    Avoids silent NaN propagation into the labeler."""
    db = _FakeDB(_bars([1000, 0, 0, 0, 0, 0]))  # 1 current + 5 baseline
    result = await fs.compute_relative_volume("NVDA", db=db)
    assert result["value"] is None
    assert result["ok"] is False
    assert result["reason"] == "baseline_mean_zero"


@pytest.mark.asyncio
async def test_rvol_computes_correctly_with_real_bars():
    """With one current + N baseline, value MUST equal
    `current_v / mean(baseline_v)`."""
    # current = 2000; baseline = [1000]*5 → mean 1000 → rvol = 2.0
    db = _FakeDB(_bars([2000, 1000, 1000, 1000, 1000, 1000]))
    result = await fs.compute_relative_volume("NVDA", db=db)
    assert result["ok"] is True
    assert result["value"] == 2.0
    assert result["current_v"] == 2000.0
    assert result["avg_v"] == 1000.0
    assert result["basis_bars"] == 5


@pytest.mark.asyncio
async def test_rvol_real_zero_is_distinct_from_none():
    """A current bar with v=0 against a real baseline returns 0.0
    (the symbol genuinely has no volume right now). This MUST be
    distinct from None (data missing). The labeler depends on the
    distinction."""
    db = _FakeDB(_bars([0, 1000, 1000, 1000, 1000, 1000]))
    result = await fs.compute_relative_volume("NVDA", db=db)
    assert result["ok"] is True
    assert result["value"] == 0.0
    assert result["current_v"] == 0.0


# ──────────────────────── has_news tripwires ────────────────────────


@pytest.mark.asyncio
async def test_has_news_none_when_api_key_missing(monkeypatch):
    """No API key → has_news=None, ok=False. Never crashes the snapshot."""
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    fs.reset_news_cache()
    result = await fs.fetch_has_news("NVDA")
    assert result["has_news"] is None
    assert result["ok"] is False
    assert result["reason"] == "finnhub_api_key_missing"


@pytest.mark.asyncio
async def test_has_news_true_when_finnhub_returns_headlines(monkeypatch):
    """Successful fetch with ≥1 headline → has_news=True, ok=True."""
    monkeypatch.setenv("FINNHUB_API_KEY", "test-key")
    fs.reset_news_cache()

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.content = b"[]"
    mock_resp.json = MagicMock(return_value=[
        {"headline": "Q3 beat"},
        {"headline": "guidance raise"},
    ])
    client = AsyncMock()
    client.get = AsyncMock(return_value=mock_resp)
    client.aclose = AsyncMock()

    result = await fs.fetch_has_news("NVDA", _http_client=client)
    assert result["ok"] is True
    assert result["has_news"] is True
    assert result["headline_count"] == 2
    assert result["source"] == "finnhub"


@pytest.mark.asyncio
async def test_has_news_false_on_successful_empty_fetch(monkeypatch):
    """Successful fetch with empty list → has_news=False (NOT None).
    Operator can trust that the empty-headlines-for-this-symbol
    signal is real, not just a fetch failure."""
    monkeypatch.setenv("FINNHUB_API_KEY", "test-key")
    fs.reset_news_cache()

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.content = b"[]"
    mock_resp.json = MagicMock(return_value=[])
    client = AsyncMock()
    client.get = AsyncMock(return_value=mock_resp)
    client.aclose = AsyncMock()

    result = await fs.fetch_has_news("BORING", _http_client=client)
    assert result["ok"] is True
    assert result["has_news"] is False
    assert result["headline_count"] == 0


@pytest.mark.asyncio
async def test_has_news_none_on_http_failure(monkeypatch):
    """4xx/5xx from Finnhub → has_news=None, ok=False. Never propagated
    as a False (which would falsely suppress news-aware labels)."""
    monkeypatch.setenv("FINNHUB_API_KEY", "test-key")
    fs.reset_news_cache()

    mock_resp = MagicMock()
    mock_resp.status_code = 500
    mock_resp.content = b""
    mock_resp.json = MagicMock(return_value={})
    client = AsyncMock()
    client.get = AsyncMock(return_value=mock_resp)
    client.aclose = AsyncMock()

    result = await fs.fetch_has_news("NVDA", _http_client=client)
    assert result["has_news"] is None
    assert result["ok"] is False
    assert "finnhub_http_500" in result["reason"]


@pytest.mark.asyncio
async def test_has_news_none_on_network_exception(monkeypatch):
    """httpx exception → has_news=None, never raised. Snapshot endpoint
    must NEVER 500 because Finnhub timed out."""
    monkeypatch.setenv("FINNHUB_API_KEY", "test-key")
    fs.reset_news_cache()

    client = AsyncMock()
    client.get = AsyncMock(side_effect=httpx.ConnectTimeout("timeout"))
    client.aclose = AsyncMock()

    result = await fs.fetch_has_news("NVDA", _http_client=client)
    assert result["has_news"] is None
    assert result["ok"] is False
    assert result["reason"].startswith("finnhub_fetch_failed:")


@pytest.mark.asyncio
async def test_has_news_cache_hit_avoids_second_fetch(monkeypatch):
    """Within TTL, the second call must NOT touch the client."""
    monkeypatch.setenv("FINNHUB_API_KEY", "test-key")
    fs.reset_news_cache()

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.content = b"[]"
    mock_resp.json = MagicMock(return_value=[{"headline": "x"}])
    client = AsyncMock()
    client.get = AsyncMock(return_value=mock_resp)
    client.aclose = AsyncMock()

    r1 = await fs.fetch_has_news("NVDA", _http_client=client)
    r2 = await fs.fetch_has_news("NVDA", _http_client=client)
    assert r1["from_cache"] is False
    assert r2["from_cache"] is True
    assert r2["has_news"] == r1["has_news"]
    # Second call must NOT have hit the network.
    assert client.get.await_count == 1


@pytest.mark.asyncio
async def test_has_news_handles_finnhub_error_dict(monkeypatch):
    """Finnhub sometimes returns {'error': 'access denied'} as a JSON
    object instead of a list when the key is bad. Must NOT be treated
    as 'has news' just because the response was non-empty."""
    monkeypatch.setenv("FINNHUB_API_KEY", "test-key")
    fs.reset_news_cache()

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.content = b'{"error": "Access denied"}'
    mock_resp.json = MagicMock(return_value={"error": "Access denied"})
    client = AsyncMock()
    client.get = AsyncMock(return_value=mock_resp)
    client.aclose = AsyncMock()

    result = await fs.fetch_has_news("NVDA", _http_client=client)
    assert result["has_news"] is None
    assert result["ok"] is False
    assert result["reason"] == "finnhub_unexpected_payload_shape"


# ──────────────────────── build_market_snapshot tripwires ────────────────────────


@pytest.mark.asyncio
async def test_snapshot_carries_per_field_ok_flags(monkeypatch):
    """Every derived field must carry its own *_ok + *_reason so the
    caller can distinguish 'unknown' from 'real value of zero/false'
    without parsing wrapper-level semantics."""
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    fs.reset_news_cache()
    db = _FakeDB([])
    snap = await fs.build_market_snapshot("NVDA", db=db)
    assert "relative_volume_ok" in snap
    assert "relative_volume_reason" in snap
    assert "has_news_ok" in snap
    assert "has_news_reason" in snap
    # Both must be None / not-ok with this empty state.
    assert snap["relative_volume"] is None
    assert snap["relative_volume_ok"] is False
    assert snap["has_news"] is None
    assert snap["has_news_ok"] is False


@pytest.mark.asyncio
async def test_snapshot_skips_news_when_requested():
    """`include_news=False` MUST not invoke the news fetch path. Avoids
    hammering Finnhub when the operator only wants RVOL."""
    db = _FakeDB([])
    snap = await fs.build_market_snapshot("NVDA", include_news=False, db=db)
    assert snap["has_news"] is None
    assert snap["has_news_reason"] == "skipped_by_caller"


# ──────────────────────── Source-scan invariants ────────────────────────


def test_feature_service_never_serves_broker_keys():
    """Defence in depth: the feature service must never reference broker
    key env vars. It reads bars + news only."""
    src = inspect.getsource(fs)
    for forbidden in (
        "ALPACA_API_KEY", "ALPACA_SECRET",
        "KRAKEN_API_KEY", "KRAKEN_SECRET",
        "IBKR_TOKEN", "BROKER_SECRET",
    ):
        assert forbidden not in src, (
            f"DOCTRINE VIOLATION: feature_service.py references broker "
            f"key {forbidden!r}. Derived evidence MUST NOT join broker "
            "authority."
        )


def test_feature_service_is_read_only_on_bars():
    """No writes to the bar collection. Feature service derives;
    bar ingestion lives in `shared/technicals.py`."""
    src = inspect.getsource(fs)
    for forbidden in (
        f'db[SHARED_OHLCV_BARS].insert',
        f'db[SHARED_OHLCV_BARS].update',
        f'db[SHARED_OHLCV_BARS].replace',
        f'db[SHARED_OHLCV_BARS].delete',
    ):
        assert forbidden not in src, (
            f"DOCTRINE VIOLATION: feature_service writes to "
            f"SHARED_OHLCV_BARS via {forbidden!r}. Bar storage is "
            "feeder-owned; feature service is read-only."
        )


# ──────────────────────── Route registration ────────────────────────


def test_snapshot_routes_registered():
    from routes import market_data_snapshot as route_mod
    paths = {r.path for r in route_mod.router.routes}
    assert "/admin/market-data/snapshot/{symbol}" in paths, paths
    assert "/admin/market-data/snapshot" in paths, paths
    assert "/admin/market-data/snapshot/cache/reset-news" in paths, paths


@pytest.mark.asyncio
async def test_dual_auth_rejects_unknown_brain():
    from routes import market_data_snapshot as route_mod
    with pytest.raises(Exception) as exc:
        await route_mod._dual_auth(
            x_brain_id="nemesis",
            x_runtime_token="anything",
            operator_user=None,
        )
    assert "unknown brain" in str(exc.value).lower()


@pytest.mark.asyncio
async def test_dual_auth_rejects_missing_principal():
    from routes import market_data_snapshot as route_mod
    with pytest.raises(Exception) as exc:
        await route_mod._dual_auth(
            x_brain_id=None,
            x_runtime_token=None,
            operator_user=None,
        )
    assert "auth required" in str(exc.value).lower()


@pytest.mark.asyncio
async def test_dual_auth_accepts_operator(monkeypatch):
    from routes import market_data_snapshot as route_mod
    principal = await route_mod._dual_auth(
        x_brain_id=None,
        x_runtime_token=None,
        operator_user={"email": "admin@risedual.io"},
    )
    assert principal == "operator:admin@risedual.io"


@pytest.mark.asyncio
async def test_dual_auth_accepts_brain(monkeypatch):
    monkeypatch.setenv("REDEYE_INGEST_TOKEN", "sek-rit")
    from routes import market_data_snapshot as route_mod
    principal = await route_mod._dual_auth(
        x_brain_id="redeye",
        x_runtime_token="sek-rit",
        operator_user=None,
    )
    assert principal == "brain:redeye"


@pytest.mark.asyncio
async def test_dual_auth_rejects_wrong_brain_token(monkeypatch):
    monkeypatch.setenv("REDEYE_INGEST_TOKEN", "sek-rit")
    from routes import market_data_snapshot as route_mod
    with pytest.raises(Exception) as exc:
        await route_mod._dual_auth(
            x_brain_id="redeye",
            x_runtime_token="wrong",
            operator_user=None,
        )
    assert "invalid token" in str(exc.value).lower()
