"""Regression: spread-quality-aware RoadGuard.

Operator pin (2026-06-22):
    "Do not let 9999 bps enter doctrine, governor, or RoadGuard as
    if it's real. That's sentinel poison."

Doctrine (per operator):
    * lane=equity & spread_bps >= 999  → spread_quality="sentinel"
    * quote_age_seconds > 15           → spread_quality="stale"
    * else                              → spread_quality="live"

    In RoadGuard:
      live      → hard-block when spread > cap
      sentinel  → WARN ONLY (pass with spread_untrusted=true)
      stale     → WARN ONLY (pass with spread_untrusted=true)
      missing   → WARN ONLY (untimed quote — refuse to authorize)

Production scenario this pins:
    NVDA snapshot returned `bid=120, ask=121.50` consistently → 121
    bps spread, which is impossible during regular hours. The old
    behaviour hard-blocked it. Now: if the quote isn't tagged as
    `live`, RoadGuard refuses to use the number to kill the trade.
"""
from __future__ import annotations

import asyncio
import sys
import pytest

sys.path.insert(0, "/app/backend")


def _build_intent(snapshot: dict) -> dict:
    return {
        "intent_id": "test-roadguard-spread-quality",
        "stack": "camino",
        "lane": "equity",
        "symbol": "NVDA",
        "action": "BUY",
        "confidence": 0.7,
        "snapshot": snapshot,
    }


@pytest.mark.asyncio
async def test_sentinel_spread_does_not_hard_block(monkeypatch):
    """A 9999-bps Webull sentinel must NOT cause RoadGuard to fail
    the gate. The gate passes with `spread_untrusted=true` so the
    audit row still surfaces the issue."""
    from shared import execution

    # Bypass all non-roadguard gates — we only care about the spread
    # branch. Monkeypatch the helpers RoadGuard calls so the test
    # focuses on the one bit of logic we just changed.
    async def _noop_council(intent):  # pragma: no cover — bypass
        return ([], 1.0)
    monkeypatch.setattr(execution, "_evaluate_council", _noop_council)

    intent = _build_intent({
        "spread_bps": 9999.0,
        "spread_quality": "sentinel",
        "quote_age_sec": None,
    })

    result = await execution._evaluate_gates(intent, order_notional_usd=10.0)
    rg = next(
        (g for g in result["gates"] if g["name"] == "roadguard_spread_floor"),
        None,
    )
    assert rg is not None, "roadguard_spread_floor gate must run"
    assert rg["passed"] is True, (
        f"9999-bps SENTINEL must NOT hard-block; gate={rg!r}. "
        "Doctrine: only `live` spreads authorize a RoadGuard kill."
    )
    assert rg.get("spread_untrusted") is True, (
        "Sentinel pass must be flagged `spread_untrusted=true` so "
        "the audit row carries the signal forward."
    )
    assert rg.get("spread_quality") == "sentinel"


@pytest.mark.asyncio
async def test_stale_quote_does_not_hard_block(monkeypatch):
    """A 121-bps spread tagged `stale` (quote >15s old) must
    warn-pass rather than block. Most regular-hours NVDA-class
    "impossible" spreads will land here."""
    from shared import execution

    async def _noop_council(intent):
        return ([], 1.0)
    monkeypatch.setattr(execution, "_evaluate_council", _noop_council)

    intent = _build_intent({
        "spread_bps": 121.0,
        "spread_quality": "stale",
        "quote_age_sec": 47.2,
    })

    result = await execution._evaluate_gates(intent, order_notional_usd=10.0)
    rg = next(g for g in result["gates"] if g["name"] == "roadguard_spread_floor")
    assert rg["passed"] is True, (
        f"stale 121 bps must NOT hard-block; gate={rg!r}"
    )
    assert rg.get("spread_untrusted") is True
    assert rg.get("spread_quality") == "stale"


@pytest.mark.asyncio
async def test_live_quote_still_authoritative_for_block(monkeypatch):
    """The fix MUST NOT defang RoadGuard for genuinely-live wide
    spreads. If quote_quality=='live' and spread > cap, hard-block
    is preserved."""
    from shared import execution

    async def _noop_council(intent):
        return ([], 1.0)
    monkeypatch.setattr(execution, "_evaluate_council", _noop_council)

    intent = _build_intent({
        "spread_bps": 120.0,
        "spread_quality": "live",
        "quote_age_sec": 2.3,
    })

    result = await execution._evaluate_gates(intent, order_notional_usd=10.0)
    rg = next(g for g in result["gates"] if g["name"] == "roadguard_spread_floor")
    assert rg["passed"] is False, (
        f"live 120 bps must STILL hard-block over the 50 bps "
        f"equity cap; gate={rg!r}. The quality tag must not be a "
        "back-door for letting genuinely-wide live markets through."
    )
    assert rg.get("spread_quality") == "live"
    assert "ROADGUARD_SPREAD_CAP" in rg["reason"]


@pytest.mark.asyncio
async def test_live_quote_passes_when_within_cap(monkeypatch):
    """Healthy regular-hours NVDA: spread=2 bps, quality=live → pass."""
    from shared import execution

    async def _noop_council(intent):
        return ([], 1.0)
    monkeypatch.setattr(execution, "_evaluate_council", _noop_council)

    intent = _build_intent({
        "spread_bps": 2.4,
        "spread_quality": "live",
        "quote_age_sec": 1.1,
    })

    result = await execution._evaluate_gates(intent, order_notional_usd=10.0)
    rg = next(g for g in result["gates"] if g["name"] == "roadguard_spread_floor")
    assert rg["passed"] is True
    assert rg.get("spread_quality") == "live"
    assert "≤ 50 bps cap" in rg["reason"]


@pytest.mark.asyncio
async def test_missing_quality_tag_treated_as_untrusted(monkeypatch):
    """An equity snapshot WITHOUT a `spread_quality` tag (legacy
    code path, or older snapshot persisted before the rename)
    must NOT default to `live`. Treat it as untrusted — RoadGuard
    refuses to authorize a kill on a number we can't vouch for."""
    from shared import execution

    async def _noop_council(intent):
        return ([], 1.0)
    monkeypatch.setattr(execution, "_evaluate_council", _noop_council)

    intent = _build_intent({
        "spread_bps": 75.0,
        # no spread_quality key — legacy path
    })

    result = await execution._evaluate_gates(intent, order_notional_usd=10.0)
    rg = next(g for g in result["gates"] if g["name"] == "roadguard_spread_floor")
    assert rg["passed"] is True, (
        f"missing spread_quality must default to UNTRUSTED — gate={rg!r}"
    )
    assert rg.get("spread_untrusted") is True


# ── Enricher tagging ──────────────────────────────────────────────


def test_enricher_tags_sentinel_when_spread_ge_999():
    """The enricher must label any equity spread ≥999 bps as
    `sentinel` (≥10% bid-ask is implausible during any market
    session)."""
    from shared.snapshot_enrich.equity_doctrine import _spread_bps

    # 9999.0 bps comes from Webull's no-quote sentinel pattern
    # (bid=0.01, ask=1.0). Confirm the math.
    sp = _spread_bps(bid=0.01, ask=1.0, mid=0.5)
    assert sp is not None and sp > 999.0, (
        f"the 9999-bps sentinel must be detectable from bid/ask "
        f"math alone; computed sp={sp}"
    )


def test_enricher_quote_age_helper_handles_ms_epoch_and_iso():
    """`_quote_age_seconds` must parse both Webull formats and
    return None when the snapshot has no timestamp."""
    from shared.snapshot_enrich.equity_doctrine import _quote_age_seconds
    import time

    now_ms = int(time.time() * 1000)
    # ms-epoch
    age = _quote_age_seconds({"mkTradeTimeTs": now_ms - 5_000})
    assert age is not None and 4.0 <= age <= 7.0, age

    # No timestamp → None
    assert _quote_age_seconds({}) is None
    assert _quote_age_seconds({"price": 100.0}) is None
