"""Sentinel-spread NO_DATA short-circuit tripwires (post-deploy fix).

Doctrine pin (2026-02-19, post-deploy operator screenshot #2):
    First fix in this session added a NO_DATA short-circuit that
    fires when the enricher explicitly failed OR the snapshot has
    none of the doctrine-facing fields. That closed the "raw brain
    ship-through" case. But it MISSED a stealthier bypass:

        `enrich_snapshot_spread` runs unconditionally on the ingest
        path. When it can't obtain a real quote, it stamps
        `spread_bps = SPREAD_BPS_UNKNOWN` (999) and
        `spread_source = "sentinel_unknown"` on the snapshot. That
        makes `spread_bps` PRESENT — so the `_no_doctrine_fields`
        guard returns False — and the doctrine proceeds to score
        against SENTINEL_SPREAD + silent defaults on every other
        field, collapsing every symbol to an IDENTICAL scored
        REJECT.

    Operator screenshot showed the exact fingerprint post-deploy:
      Strategist Δ=-0.03 · Auditor 1 obj cs=0.50 · Governor mult=0.65 ·
      Executor -80% (3 checks failed) — IDENTICAL across every
      brain / every symbol / every intent. Different from the pre-
      deploy fingerprint (Δ=-0.12/-0.26, 3 objs cs=0.74, mult=0.15),
      but same bug class.

    Fix: extend the short-circuit to detect
    `spread_source == "sentinel_unknown"` as equivalent to no data.
    Applied symmetrically to both `large_cap_doctrine.py` and
    `base_labels.py` — they must agree on what "populated snapshot"
    means or one doctrine will keep manufacturing verdicts on
    empty data while the other refuses.
"""
from __future__ import annotations

import pytest

from shared.doctrine.base_labels import build_doctrine_labels
from shared.doctrine.large_cap_doctrine import build_large_cap_doctrine_packet


def _sentinel_snapshot(symbol: str = "NVDA", **extra) -> dict:
    """Reproduce the exact shape the `enrich_snapshot_spread`
    sentinel path leaves on the snapshot before doctrine runs.
    `spread_bps=999` and `spread_source="sentinel_unknown"` are
    the sentinel constants — any real market data would have
    `spread_source` in {`brain`, `mc_derived`, `mc_indicator_cache`,
    `mc_kraken`} and a realistic spread_bps."""
    snap = {
        "lane": "equity",
        "symbol": symbol,
        "market_cap_band": "mega",
        "spread_bps": 999,
        "spread_source": "sentinel_unknown",
    }
    snap.update(extra)
    return snap


@pytest.mark.tripwire
def test_large_cap_short_circuits_on_sentinel_spread():
    """Sentinel-spread snapshot → NO_DATA (not scored REJECT)."""
    packet = build_large_cap_doctrine_packet(_sentinel_snapshot("NVDA"))
    assert packet["base_labels"]["quality"] == "NO_DATA"
    assert packet["base_labels"]["score"] == 0.0
    # Every seat neutral, no scored penalties.
    for seat_name in ("strategist", "adversary", "governor", "execution_judge"):
        assert packet["seats"][seat_name].get("no_data") is True, seat_name
    assert packet["seats"]["governor"]["risk_multiplier"] == 1.0
    assert packet["seats"]["strategist"]["conviction_delta"] == 0.0


@pytest.mark.tripwire
def test_small_cap_short_circuits_on_sentinel_spread():
    """Same invariant for `base_labels.build_doctrine_labels`."""
    labels = build_doctrine_labels(_sentinel_snapshot("SPCE"))
    assert labels.quality == "NO_DATA"
    assert labels.score == 0.0
    assert "ENRICHMENT_UNAVAILABLE" in labels.labels
    assert any("sentinel_spread" in r for r in labels.reasons)


@pytest.mark.tripwire
def test_operator_screenshot_2_reproduction_is_fixed():
    """Direct regression test for the 2026-02-19 post-deploy screenshot.

    Four symbols across four brains, all with the sentinel-spread
    snapshot shape the spread enricher stamps in the live path.
    Under the buggy code every packet collapsed to the same scored
    REJECT with the '-0.03 / -0.50 / 0.65 / -80%' fingerprint. Under
    the fix all four must return NO_DATA with the neutral-seat
    invariants.
    """
    packets = [
        build_large_cap_doctrine_packet(
            _sentinel_snapshot(sym),
            seat_holders={
                "strategist": "camino", "auditor": "barracuda",
                "governor": "hellcat", "executor": "gto",
            },
        )
        for sym in ("AMZN", "MSFT", "TSLA", "NVDA")
    ]
    for p in packets:
        assert p["base_labels"]["quality"] == "NO_DATA"
        # No conviction/objection/multiplier variance across symbols.
        assert p["seats"]["strategist"]["conviction_delta"] == 0.0
        assert p["seats"]["adversary"]["challenge_strength"] == 0.0
        assert p["seats"]["adversary"]["objections"] == []
        assert p["seats"]["governor"]["risk_multiplier"] == 1.0
        assert p["seats"]["execution_judge"]["execution_ready"] is None


@pytest.mark.tripwire
def test_real_spread_source_still_scores_normally():
    """Sanity: presence of a real (non-sentinel) `spread_source`
    with full doctrine fields still runs the scored path. This is
    the regression guard so the sentinel check doesn't accidentally
    swallow legitimate populated snapshots."""
    real = {
        "lane": "equity", "symbol": "NVDA", "market_cap_band": "mega",
        "spread_bps": 8, "spread_source": "mc_indicator_cache",
        "price": 850.0, "gap_pct": 1.2, "relative_volume": 2.0,
        "market_regime": "strong",
    }
    packet = build_large_cap_doctrine_packet(real)
    assert packet["base_labels"]["quality"] != "NO_DATA"
    # Seats must NOT be flagged as no_data.
    assert packet["seats"]["strategist"].get("no_data") is not True


@pytest.mark.tripwire
def test_missing_spread_source_treated_as_absence_not_sentinel():
    """A snapshot with `spread_bps` but no `spread_source` at all
    is NOT the sentinel case (that's the brain-shipped raw
    snapshot). It falls through to normal scoring, which for a
    populated-enough snapshot is fine. Distinguishes 'sentinel
    stamped by MC' from 'no source field at all'.
    """
    # Sneaky shape: has spread_bps but no source. Should score.
    snap = {
        "lane": "equity", "symbol": "NVDA", "market_cap_band": "mega",
        "spread_bps": 8, "price": 850.0, "gap_pct": 1.2,
        "relative_volume": 2.0, "market_regime": "strong",
    }
    packet = build_large_cap_doctrine_packet(snap)
    # Not NO_DATA — no sentinel-source flag, and doctrine fields
    # are populated.
    assert packet["base_labels"]["quality"] != "NO_DATA"
