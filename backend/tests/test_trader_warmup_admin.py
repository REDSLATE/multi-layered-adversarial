"""Tests for /api/admin/trader/warmup-progress (2026-07-03).

Contract locked here:
    * Reports per-symbol bar counts across the configured universe
    * `ready=True` iff bars ≥ 50 (matches research warmup floor)
    * `all_ready` is the AND of every symbol's `ready`
    * Soft-degrades on Atlas timeout / exception (never leaks raw
      NetworkTimeout) — same envelope shape as /parabolic-phase/phases
    * Empty universe → all_ready=True (nothing to warm)
    * Backward-compat: uses singular env vars if plural aren't set

The endpoint is deliberately SEPARATE from /status so /status can
keep its "no Atlas dependency" promise.
"""
from __future__ import annotations

import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, "/app")
sys.path.insert(0, "/app/backend")


def _make_db(bar_counts: dict[tuple[str, str], int]) -> MagicMock:
    coll = MagicMock()

    async def _count(q):
        key = (q["symbol"], q["tf"])
        return bar_counts.get(key, 0)

    coll.count_documents = AsyncMock(side_effect=_count)
    db_mock = MagicMock()
    db_mock.__getitem__ = MagicMock(return_value=coll)
    return db_mock


@pytest.mark.asyncio
async def test_all_ready_when_every_symbol_has_50_plus_bars(monkeypatch):
    from routes import trader_warmup_admin
    monkeypatch.setenv("TRADER_EQUITY_TICKERS", "NVDA,SPY")
    monkeypatch.setenv("TRADER_CRYPTO_PAIRS", "XBTUSD,SOLUSD")
    monkeypatch.setattr(trader_warmup_admin, "db", _make_db({
        ("NVDA", "1d"): 60, ("SPY", "1d"): 55,
        ("XBTUSD", "1h"): 75, ("SOLUSD", "1h"): 50,
    }))
    r = await trader_warmup_admin.warmup_progress(_={})
    assert r["all_ready"] is True
    assert r["ready_count"] == 4
    assert r["total_symbols"] == 4
    for s in r["symbols"]:
        assert s["ready"] is True
        assert s["bars"] >= 50


@pytest.mark.asyncio
async def test_partial_readiness_reports_not_ready_first(monkeypatch):
    """Not-ready symbols must sort before ready ones so the operator
    sees the blockers at the top."""
    from routes import trader_warmup_admin
    monkeypatch.setenv("TRADER_EQUITY_TICKERS", "NVDA,SPY")
    monkeypatch.setenv("TRADER_CRYPTO_PAIRS", "XBTUSD,SOLUSD")
    monkeypatch.setattr(trader_warmup_admin, "db", _make_db({
        ("NVDA", "1d"): 60, ("SPY", "1d"): 9,        # SPY still warming
        ("XBTUSD", "1h"): 75, ("SOLUSD", "1h"): 12,  # SOL still warming
    }))
    r = await trader_warmup_admin.warmup_progress(_={})
    assert r["all_ready"] is False
    assert r["ready_count"] == 2
    # First two entries must be the not-ready ones.
    assert r["symbols"][0]["ready"] is False
    assert r["symbols"][1]["ready"] is False


@pytest.mark.asyncio
async def test_pct_complete_boundaries(monkeypatch):
    from routes import trader_warmup_admin
    monkeypatch.setenv("TRADER_EQUITY_TICKERS", "AAA,BBB,CCC")
    monkeypatch.delenv("TRADER_CRYPTO_PAIRS", raising=False)
    monkeypatch.setenv("TRADER_CRYPTO_PAIR", "XBTUSD")
    monkeypatch.setattr(trader_warmup_admin, "db", _make_db({
        ("AAA", "1d"): 0,   # 0% complete
        ("BBB", "1d"): 25,  # 50%
        ("CCC", "1d"): 100, # 200% → clamped to 100
        ("XBTUSD", "1h"): 60,
    }))
    r = await trader_warmup_admin.warmup_progress(_={})
    pcts = {s["symbol"]: s["pct_complete"] for s in r["symbols"]}
    assert pcts["AAA"] == 0
    assert pcts["BBB"] == 50
    assert pcts["CCC"] == 100     # capped, not 200
    assert pcts["XBTUSD"] == 100


@pytest.mark.asyncio
async def test_soft_degrade_on_atlas_timeout(monkeypatch):
    """Endpoint must never leak raw NetworkTimeout — soft-error envelope
    with error='mongo_timeout' and empty symbols list."""
    from routes import trader_warmup_admin
    monkeypatch.setenv("TRADER_EQUITY_TICKERS", "NVDA")
    monkeypatch.setattr(trader_warmup_admin, "_MONGO_READ_TIMEOUT_S", 0.1)
    coll = MagicMock()

    async def _slow_count(_q):
        await asyncio.sleep(5)   # much longer than 0.1s timeout
        return 100
    coll.count_documents = AsyncMock(side_effect=_slow_count)
    db_mock = MagicMock()
    db_mock.__getitem__ = MagicMock(return_value=coll)
    monkeypatch.setattr(trader_warmup_admin, "db", db_mock)

    r = await trader_warmup_admin.warmup_progress(_={})
    assert r["ok"] is True
    assert r["error"] == "mongo_timeout"
    assert r["all_ready"] is False
    assert r["symbols"] == []
    assert "timed out" in r["message"].lower()


@pytest.mark.asyncio
async def test_soft_degrade_on_atlas_exception(monkeypatch):
    from routes import trader_warmup_admin
    monkeypatch.setenv("TRADER_EQUITY_TICKERS", "NVDA")
    coll = MagicMock()

    async def _raise(_q):
        raise RuntimeError("NetworkTimeout: Atlas read failed")
    coll.count_documents = AsyncMock(side_effect=_raise)
    db_mock = MagicMock()
    db_mock.__getitem__ = MagicMock(return_value=coll)
    monkeypatch.setattr(trader_warmup_admin, "db", db_mock)

    r = await trader_warmup_admin.warmup_progress(_={})
    assert r["error"] == "mongo_error"
    assert "RuntimeError" in r["message"]


@pytest.mark.asyncio
async def test_empty_universe_all_ready(monkeypatch):
    """If no symbols are configured at all, nothing to warm — all_ready
    is True (vacuous truth), not False."""
    from routes import trader_warmup_admin
    # Delete plural env; set singular to empty string so the fallback
    # ALSO produces no symbols.
    monkeypatch.delenv("TRADER_EQUITY_TICKERS", raising=False)
    monkeypatch.delenv("TRADER_CRYPTO_PAIRS", raising=False)
    monkeypatch.setenv("TRADER_EQUITY_TICKER", "")
    monkeypatch.setenv("TRADER_CRYPTO_PAIR", "")

    # Override the universe helper so the test doesn't depend on
    # the singular-fallback quirk.
    monkeypatch.setattr(
        trader_warmup_admin, "_configured_universe", lambda: [],
    )
    monkeypatch.setattr(trader_warmup_admin, "db", _make_db({}))
    r = await trader_warmup_admin.warmup_progress(_={})
    assert r["all_ready"] is True
    assert r["symbols"] == []


@pytest.mark.asyncio
async def test_backward_compat_singular_env_vars(monkeypatch):
    """When only the singular env vars are set (Stage-0 deploy),
    the endpoint must still work — no crash, one symbol per lane."""
    from routes import trader_warmup_admin
    monkeypatch.delenv("TRADER_EQUITY_TICKERS", raising=False)
    monkeypatch.delenv("TRADER_CRYPTO_PAIRS", raising=False)
    monkeypatch.setenv("TRADER_EQUITY_TICKER", "TSLA")
    monkeypatch.setenv("TRADER_CRYPTO_PAIR", "XBTUSD")
    monkeypatch.setattr(trader_warmup_admin, "db", _make_db({
        ("TSLA", "1d"): 60, ("XBTUSD", "1h"): 75,
    }))
    r = await trader_warmup_admin.warmup_progress(_={})
    assert r["all_ready"] is True
    assert r["total_symbols"] == 2
    symbols = {s["symbol"] for s in r["symbols"]}
    assert symbols == {"TSLA", "XBTUSD"}


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
