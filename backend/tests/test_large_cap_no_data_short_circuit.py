"""Large-cap NO_DATA short-circuit tripwires.

Doctrine pin (2026-02-19, operator directive after live UI evidence):
    Operator screenshot showed every intent card for AMZN/MSFT
    (regardless of brain) rendering IDENTICAL scored numbers:
      * Execution 0% · threshold 50% · missed by 50%
      * Strategist -12% (Δ=-0.12)
      * Auditor -30% (3 objs, cs=0.74 required)
      * Governor -85% (RISK_DOWN, mult=0.15)
      * Executor -80% (3 checks failed)
      * Doctrine REJECT · score 0.35 · large_cap_doctrine_reject
    Root cause: snapshots arrived with `NO_PROVENANCE` (enricher
    hadn't populated ANY doctrine fields), and the large-cap
    doctrine scored them against silent defaults — spread_bps=999
    triggered SPREAD_TOO_WIDE, everything else was silent, seats
    scored against a `_LargeCapLabels(score=0.0)` and collapsed to
    the same numbers on every symbol. Exact same silent-default
    bug class we vetoed on the classifier side earlier the same
    session; the doctrine side was missing the symmetric short-
    circuit that `base_labels.py` + `brain_sidecars.py` already had.

    These tripwires pin the fix: when the enricher marks failure
    OR the snapshot has zero doctrine fields, the packet MUST
    return quality="NO_DATA" with `no_data: True` on every seat —
    NEVER a scored REJECT.
"""
from __future__ import annotations

import pytest

from shared.doctrine.large_cap_doctrine import build_large_cap_doctrine_packet


@pytest.mark.tripwire
def test_no_data_when_enrichment_status_failed():
    """Explicit enricher-failure flag → NO_DATA packet."""
    packet = build_large_cap_doctrine_packet(
        {"lane": "equity", "symbol": "NVDA", "enrichment_status": "failed"},
        seat_holders=None,
    )
    assert packet["base_labels"]["quality"] == "NO_DATA"
    for seat in ("strategist", "adversary", "governor", "execution_judge"):
        assert packet["seats"][seat]["no_data"] is True, seat
        assert packet["seats"][seat]["may_execute"] is False, seat


@pytest.mark.tripwire
def test_no_data_when_enrichment_status_no_symbol():
    packet = build_large_cap_doctrine_packet(
        {"lane": "equity", "symbol": "NVDA", "enrichment_status": "no_symbol"},
        seat_holders=None,
    )
    assert packet["base_labels"]["quality"] == "NO_DATA"


@pytest.mark.tripwire
def test_no_data_when_snapshot_has_no_doctrine_fields():
    """A snapshot with only `lane` + `symbol` (i.e., the enricher
    silently failed to add anything) must NOT be scored — must
    short-circuit to NO_DATA. This is the exact live-bug shape
    that produced the operator's identical-numbers screenshot."""
    packet = build_large_cap_doctrine_packet(
        {"lane": "equity", "symbol": "MSFT", "market_cap_band": "mega"},
        seat_holders=None,
    )
    assert packet["base_labels"]["quality"] == "NO_DATA"
    # Score MUST be 0.0, not the old baseline (0.30/0.35/0.40).
    assert packet["base_labels"]["score"] == 0.0
    # Direction MUST be neutral with an explicit no-data reason.
    assert packet["direction"]["strategy_bias"] == "NEUTRAL"
    assert packet["direction"]["bias_strength"] == 0.0
    assert "no_data" in packet["direction"]["bias_reasons"][0]


@pytest.mark.tripwire
def test_no_data_seats_are_neutral_not_penalizing():
    """Every seat in a NO_DATA packet must return neutral values,
    NOT the score-driven penalties (conviction_delta<0,
    risk_multiplier<1, objections filled). This is the operator's
    exact complaint: 'exact same numbers no matter the symbol.'"""
    packet = build_large_cap_doctrine_packet(
        {"lane": "equity", "symbol": "AMZN"},
        seat_holders=None,
    )
    seats = packet["seats"]
    assert seats["strategist"]["conviction_delta"] == 0.0
    assert seats["adversary"]["challenge_required"] is False
    assert seats["adversary"]["challenge_strength"] == 0.0
    assert seats["adversary"]["objections"] == []
    assert seats["governor"]["risk_multiplier"] == 1.0
    assert seats["governor"]["governor_action"] == "modulate"
    assert seats["governor"]["display_status"] == "NO_DATA"
    assert seats["execution_judge"]["execution_ready"] is None
    assert seats["execution_judge"]["execution_checks"] == {}


@pytest.mark.tripwire
def test_no_data_holders_are_populated_when_provided():
    """The short-circuit must still respect the seat-holder mapping
    so the UI shows who WOULD have scored if data were present."""
    holders = {
        "strategist": "camino",
        "auditor": "barracuda",
        "governor": "hellcat",
        "executor": "gto",
    }
    packet = build_large_cap_doctrine_packet(
        {"lane": "equity", "symbol": "TSLA"},
        seat_holders=holders,
    )
    seats = packet["seats"]
    assert seats["strategist"]["holder"] == "camino"
    assert seats["adversary"]["holder"] == "barracuda"
    assert seats["governor"]["holder"] == "hellcat"
    assert seats["execution_judge"]["holder"] == "gto"


@pytest.mark.tripwire
def test_populated_snapshot_still_scores_normally():
    """Sanity: presence of doctrine fields still runs the scored
    path. Regression guard so the NO_DATA gate doesn't accidentally
    swallow real snapshots (e.g., if the field-detection tuple ever
    misses a legitimate signal)."""
    packet = build_large_cap_doctrine_packet(
        {
            "lane": "equity", "symbol": "NVDA",
            # Any one of the tracked fields present is enough.
            "gap_pct": 1.2, "relative_volume": 2.0,
            "spread_bps": 8, "market_regime": "strong",
            "price": 850.0,
        },
        seat_holders=None,
    )
    assert packet["base_labels"]["quality"] != "NO_DATA"
    assert packet["seats"]["strategist"].get("no_data") is not True


@pytest.mark.tripwire
def test_operator_screenshot_bug_reproduction_is_fixed():
    """Direct regression test for the 2026-02-19 operator screenshot.

    Four different symbols (AMZN, MSFT, TSLA, NVDA), no enricher
    payload — under the old code all four produced IDENTICAL scored
    REJECTs. Under the fix all four must be NO_DATA (and MAY vary in
    holder/symbol but MUST NOT vary in seat scoring since there's
    literally nothing to score).
    """
    packets = [
        build_large_cap_doctrine_packet(
            {"lane": "equity", "symbol": sym}, seat_holders=None,
        )
        for sym in ("AMZN", "MSFT", "TSLA", "NVDA")
    ]
    for p in packets:
        assert p["base_labels"]["quality"] == "NO_DATA"
        assert p["base_labels"]["score"] == 0.0
        assert p["direction"]["strategy_bias"] == "NEUTRAL"
        # No manufactured scored objections/penalties.
        assert p["seats"]["strategist"]["conviction_delta"] == 0.0
        assert p["seats"]["governor"]["risk_multiplier"] == 1.0
