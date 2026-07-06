"""Regression: doctrine advisory must NOT manufacture a REJECT verdict
from all-default snapshot values.

Operator context (2026-07-06):
    Diagnostics UI showed AMH / AII / AFG with bit-for-bit identical
    scorecard rows: strategist -26%, auditor -38%, governor -88%,
    executor -80%. Root cause — when the equity Webull enricher fails
    (or the snapshot never carries the doctrine-facing fields),
    `base_labels.py` defaults every field to a value that fails its
    band check:

        price          = 0.0         → no SMALL_ACCOUNT_PRICE_VALID
        gap_pct        = 0.0         → no GAPPER
        relative_volume= 0.0         → no HIGH_RELATIVE_VOLUME
        has_news       = False       → NO_NEWS_RISK penalty
        float_millions = 999999.0    → no LOW_FLOAT
        spread_bps     = 999.0       → SPREAD_TOO_WIDE (-0.15)

    Cascading through the seat builders yields the exact fingerprint
    the operator saw — regardless of symbol.

Fix (this test locks it):
    1. Enricher stamps `enrichment_status ∈ {live, failed, no_symbol}`
       and lists Webull-unavailable fields in `enrichment_unavailable_fields`.
    2. `build_doctrine_labels` short-circuits to `quality="NO_DATA"`
       when the enricher failed OR the snapshot has none of the
       doctrine-facing fields.
    3. `build_all_brain_doctrine_packets` returns neutral seat bodies
       (conviction_delta=0, no objections, risk_multiplier=1.0,
       execution_ready=None) with `no_data=True` flags so the UI can
       render an honest "no data" panel.
    4. When `has_news` / `float_millions` are marked unavailable, the
       labeler emits informational `NEWS_DATA_UNAVAILABLE` /
       `FLOAT_DATA_UNAVAILABLE` labels — NO score penalty for
       absence-of-data vs. adverse-data.
"""
from __future__ import annotations

import pytest

from shared.doctrine.base_labels import build_doctrine_labels
from shared.doctrine.brain_sidecars import build_all_brain_doctrine_packets


class TestNoDataShortCircuit:
    def test_empty_snapshot_yields_no_data_not_reject(self):
        """The bug fingerprint: {symbol, lane} only.

        Pre-fix this produced quality=REJECT with the -26/-38/-88/-80
        cascade. Post-fix: quality=NO_DATA, no cascade."""
        labels = build_doctrine_labels({"symbol": "AMH", "lane": "equity"})
        assert labels.quality == "NO_DATA"
        assert "ENRICHMENT_UNAVAILABLE" in labels.labels
        assert labels.score == 0.0
        assert any("no_data" in r for r in labels.reasons)

    def test_explicit_enrichment_failed_yields_no_data(self):
        labels = build_doctrine_labels({
            "symbol": "AMH", "lane": "equity",
            "enrichment_status": "failed",
            "enrichment_error": "RuntimeError('webull creds missing')",
        })
        assert labels.quality == "NO_DATA"
        assert labels.reasons == ["no_data:failed"]

    def test_no_symbol_enrichment_yields_no_data(self):
        labels = build_doctrine_labels({
            "symbol": "AMH", "lane": "equity",
            "enrichment_status": "no_symbol",
        })
        assert labels.quality == "NO_DATA"
        assert labels.reasons == ["no_data:no_symbol"]

    @pytest.mark.parametrize("symbol", ["AMH", "AII", "AFG", "TSLA", "SPY"])
    def test_identical_output_for_every_symbol_when_no_data(self, symbol):
        """Locks in that the panel doesn't LOOK per-symbol when it's
        actually rendering a default. Verifies NO_DATA fires for any
        equity symbol with no doctrine fields — the operator sees the
        honest "no data" branch, not a fake per-symbol verdict."""
        packet = build_all_brain_doctrine_packets(
            {"symbol": symbol, "lane": "equity"}, {},
        )
        assert packet["base_labels"]["quality"] == "NO_DATA"
        assert packet["seats"]["strategist"]["no_data"] is True
        assert packet["seats"]["adversary"]["no_data"] is True
        assert packet["seats"]["governor"]["no_data"] is True
        assert packet["seats"]["execution_judge"]["no_data"] is True

    def test_no_data_packet_has_neutral_seat_values(self):
        """The seats must NOT emit adversarial defaults when there's
        no data. The old bug had strategist Δ=-0.26, cs=0.92, mult=0.13
        — those numbers came out because REJECT quality cascaded.
        Under NO_DATA: everything neutral."""
        packet = build_all_brain_doctrine_packets(
            {"symbol": "AMH", "lane": "equity"}, {},
        )
        s = packet["seats"]["strategist"]
        a = packet["seats"]["adversary"]
        g = packet["seats"]["governor"]
        e = packet["seats"]["execution_judge"]

        assert s["conviction_delta"] == 0.0
        assert a["challenge_required"] is False
        assert a["challenge_strength"] == 0.0
        assert a["objections"] == []
        assert g["risk_multiplier"] == 1.0
        assert g["display_status"] == "NO_DATA"
        assert g["block_reasons"] == []
        assert g["execution_effect"] == "NO_DATA"
        assert e["execution_ready"] is None  # not False — "unknown"

    def test_governor_display_is_no_data_not_risk_down(self):
        """Prevents regression to the misleading `RISK_DOWN doctrine_reject`
        chip the operator saw for AMH/AII/AFG."""
        packet = build_all_brain_doctrine_packets(
            {"symbol": "AMH", "lane": "equity"}, {},
        )
        g = packet["seats"]["governor"]
        assert g["display_status"] == "NO_DATA"
        assert g["reason"] is None
        assert "doctrine_reject" not in (g.get("block_reasons") or [])


class TestUnavailableFieldsAreInformational:
    """When enrichment succeeds but a field is documented as
    unavailable-under-entitlement (has_news, float_millions on Webull),
    absence must NOT be scored as adverse data."""

    def _real_snapshot(self, **overrides):
        base = {
            "symbol": "NVDA", "lane": "equity",
            "price": 9.50, "gap_pct": 12.0, "relative_volume": 6.5,
            "float_millions": 999999.0,  # default — should be treated as unknown
            "spread_bps": 25.0, "spread_quality": "live",
            "market_regime": "strong", "pattern": "micro_pullback",
            "enrichment_status": "live",
            "enrichment_unavailable_fields": ["has_news", "float_millions"],
        }
        base.update(overrides)
        return base

    def test_has_news_unavailable_does_not_add_no_news_risk(self):
        labels = build_doctrine_labels(self._real_snapshot())
        assert "NO_NEWS_RISK" not in labels.labels
        assert "NEWS_DATA_UNAVAILABLE" in labels.labels
        assert "no_news_catalyst" not in labels.reasons

    def test_float_unavailable_does_not_add_float_above_20m(self):
        labels = build_doctrine_labels(self._real_snapshot())
        assert "FLOAT_DATA_UNAVAILABLE" in labels.labels
        # Neither the positive LOW_FLOAT nor the negative float_above_20m
        # reason should appear when the source is marked unavailable.
        assert "LOW_FLOAT_SUPPLY_IMBALANCE" not in labels.labels
        assert "float_above_20m" not in labels.reasons

    def test_real_data_still_scores_normally(self):
        """Sanity check: unavailability flags don't accidentally suppress
        legitimate signals. A gap-and-go with catalyst + low float still
        scores when has_news / float are actually populated + NOT flagged
        as unavailable."""
        snap = self._real_snapshot(
            has_news=True,
            float_millions=15.0,
            enrichment_unavailable_fields=[],  # both fields populated
        )
        labels = build_doctrine_labels(snap)
        assert labels.quality in {"A_QUALITY", "B_QUALITY"}
        assert "NEWS_CATALYST" in labels.labels
        assert "LOW_FLOAT_SUPPLY_IMBALANCE" in labels.labels


class TestOldBugFingerprintCannotReturn:
    """Direct anti-regression: the exact -26/-38/-88/-80 fingerprint
    the operator saw in the AMH/AII/AFG screenshot must never appear
    again for an empty-snapshot input."""

    @pytest.mark.parametrize("symbol", ["AMH", "AII", "AFG"])
    def test_old_reject_cascade_no_longer_fires(self, symbol):
        packet = build_all_brain_doctrine_packets(
            {"symbol": symbol, "lane": "equity"}, {},
        )
        s = packet["seats"]["strategist"]
        a = packet["seats"]["adversary"]
        g = packet["seats"]["governor"]

        # The pre-fix values the operator saw:
        BAD_STRATEGIST_DELTA = -0.26
        BAD_CHALLENGE_STRENGTH = 0.92
        BAD_GOVERNOR_MULT = 0.125

        assert s["conviction_delta"] != BAD_STRATEGIST_DELTA
        assert a["challenge_strength"] != BAD_CHALLENGE_STRENGTH
        assert g["risk_multiplier"] != BAD_GOVERNOR_MULT
        assert a["objections"] != [
            "move_not_news_backed", "spread_risk",
            "setup_quality_insufficient",
            "supply_imbalance_not_confirmed",
        ]
