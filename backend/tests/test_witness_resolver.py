"""Regression suite for the witness W/L resolver.

Locks in the deterministic core (classification, aggregation,
promotion transitions) so the resolver's arithmetic can't drift
silently. The DB-touching path is exercised via a fake price fetcher
so tests are hermetic and don't require Webull/Kraken access.
"""
from __future__ import annotations

import pytest

from verifier.witness_resolver import (
    HOLD_WINDOW_BPS,
    MIN_MOVE_BPS_FOR_DIRECTIONAL_WIN,
    SourceAggregate,
    classify_outcome,
    next_status,
)


# ─────────────────────────── classify_outcome ───────────────────────────


class TestClassifyOutcome:
    def test_buy_wins_when_return_meets_threshold(self):
        assert classify_outcome("BUY", MIN_MOVE_BPS_FOR_DIRECTIONAL_WIN) == "win"
        assert classify_outcome("BUY", MIN_MOVE_BPS_FOR_DIRECTIONAL_WIN + 100) == "win"

    def test_buy_loses_below_threshold(self):
        assert classify_outcome("BUY", MIN_MOVE_BPS_FOR_DIRECTIONAL_WIN - 1) == "loss"
        assert classify_outcome("BUY", 0) == "loss"
        assert classify_outcome("BUY", -500) == "loss"

    def test_sell_wins_on_downside_move(self):
        assert classify_outcome("SELL", -MIN_MOVE_BPS_FOR_DIRECTIONAL_WIN) == "win"
        assert classify_outcome("SELL", -1000) == "win"

    def test_sell_loses_flat_or_up(self):
        assert classify_outcome("SELL", -(MIN_MOVE_BPS_FOR_DIRECTIONAL_WIN - 1)) == "loss"
        assert classify_outcome("SELL", 0) == "loss"
        assert classify_outcome("SELL", 200) == "loss"

    def test_hold_wins_in_the_quiet_window(self):
        assert classify_outcome("HOLD", 0) == "win"
        assert classify_outcome("HOLD", HOLD_WINDOW_BPS - 1) == "win"
        assert classify_outcome("HOLD", -(HOLD_WINDOW_BPS - 1)) == "win"

    def test_hold_loses_when_market_moves(self):
        assert classify_outcome("HOLD", HOLD_WINDOW_BPS + 1) == "loss"
        assert classify_outcome("HOLD", -(HOLD_WINDOW_BPS + 1)) == "loss"

    def test_unknown_side_is_undetermined(self):
        # Type-hint says Literal, but runtime rows may have drift
        # (schema migration in progress, etc.). Resolver must not
        # invent a classification for a stance it doesn't understand.
        assert classify_outcome("MAYBE", 100) == "undetermined"  # type: ignore[arg-type]


# ─────────────────────────── SourceAggregate ───────────────────────────


class TestSourceAggregate:
    def test_empty_aggregate_has_zero_metrics(self):
        agg = SourceAggregate(source="polygon")
        assert agg.samples == 0
        assert agg.orthogonal_win_rate == 0.0
        assert agg.verified_alpha == 0.0
        assert agg.avg_return_bps == 0.0

    def test_add_win_increments_wins_and_samples(self):
        agg = SourceAggregate(source="polygon")
        agg.add("win", 75.0)
        assert agg.samples == 1
        assert agg.wins == 1
        assert agg.losses == 0
        assert agg.orthogonal_win_rate == 1.0

    def test_add_loss_increments_losses_and_samples(self):
        agg = SourceAggregate(source="polygon")
        agg.add("loss", -30.0)
        assert agg.samples == 1
        assert agg.wins == 0
        assert agg.losses == 1
        assert agg.orthogonal_win_rate == 0.0

    def test_undetermined_does_not_alter_counts(self):
        agg = SourceAggregate(source="polygon")
        agg.add("undetermined", 999.0)
        assert agg.samples == 0
        assert agg.wins == 0
        assert agg.losses == 0

    def test_verified_alpha_is_avg_return_over_10000(self):
        agg = SourceAggregate(source="polygon")
        agg.add("win", 200.0)   # +200 bps
        agg.add("loss", -100.0) # -100 bps
        # avg = 50 bps → verified_alpha = 0.005
        assert agg.avg_return_bps == pytest.approx(50.0)
        assert agg.verified_alpha == pytest.approx(0.005)


# ─────────────────────────── next_status transitions ───────────────────────────


class TestPromotionTransitions:
    def test_untrusted_stays_untrusted_below_thresholds(self):
        assert next_status("UNTRUSTED", samples=0, orthogonal_win_rate=0.0,
                           verified_alpha=0.0) == "UNTRUSTED"
        # Win-rate path fails on sample count; alpha path fails on sample count.
        assert next_status("UNTRUSTED", samples=49, orthogonal_win_rate=0.99,
                           verified_alpha=0.5) == "UNTRUSTED"
        # Win-rate path fails on strict-greater bound; alpha path fails on
        # sample count (needs 100).
        assert next_status("UNTRUSTED", samples=50, orthogonal_win_rate=0.50,
                           verified_alpha=0.0) == "UNTRUSTED"

    def test_untrusted_promotes_to_watchlist_at_threshold(self):
        # Exactly the pinned doctrine: samples≥50 AND win_rate>0.50
        assert next_status("UNTRUSTED", samples=50, orthogonal_win_rate=0.51,
                           verified_alpha=0.0) == "WATCHLIST"
        assert next_status("UNTRUSTED", samples=500, orthogonal_win_rate=0.60,
                           verified_alpha=0.0) == "WATCHLIST"

    # ─── Alpha-based WATCHLIST pathway (2026-02-19 doctrine addition) ───
    def test_untrusted_promotes_via_alpha_path(self):
        # Sub-50% win rate but strong positive expectancy →
        # WATCHLIST via the alpha pathway. Polygon at 24h looked
        # exactly like this: ~41% win rate + 70 bps alpha.
        assert next_status("UNTRUSTED", samples=100, orthogonal_win_rate=0.41,
                           verified_alpha=0.007) == "WATCHLIST"

    def test_untrusted_alpha_path_needs_100_samples(self):
        # 99 samples: below alpha-path floor even with strong alpha.
        assert next_status("UNTRUSTED", samples=99, orthogonal_win_rate=0.41,
                           verified_alpha=0.02) == "UNTRUSTED"
        # 100 exactly: promotes.
        assert next_status("UNTRUSTED", samples=100, orthogonal_win_rate=0.41,
                           verified_alpha=0.005) == "WATCHLIST"

    def test_untrusted_alpha_path_needs_50bps(self):
        # 49 bps: below the 50 bps alpha floor.
        assert next_status("UNTRUSTED", samples=500, orthogonal_win_rate=0.41,
                           verified_alpha=0.0049) == "UNTRUSTED"
        # 50 bps exactly: promotes (≥, not strict-greater).
        assert next_status("UNTRUSTED", samples=500, orthogonal_win_rate=0.41,
                           verified_alpha=0.005) == "WATCHLIST"

    def test_untrusted_negative_alpha_never_promotes(self):
        # Even with lots of samples and mediocre-but-not-terrible
        # win rate, negative alpha keeps the source UNTRUSTED.
        assert next_status("UNTRUSTED", samples=1000, orthogonal_win_rate=0.45,
                           verified_alpha=-0.01) == "UNTRUSTED"

    def test_watchlist_promotes_to_trusted_at_threshold(self):
        assert next_status("WATCHLIST", samples=200, orthogonal_win_rate=0.51,
                           verified_alpha=0.021) == "TRUSTED"

    def test_watchlist_does_not_promote_below_sample_floor(self):
        assert next_status("WATCHLIST", samples=199, orthogonal_win_rate=0.90,
                           verified_alpha=0.10) == "WATCHLIST"

    def test_watchlist_does_not_promote_without_positive_alpha(self):
        assert next_status("WATCHLIST", samples=500, orthogonal_win_rate=0.51,
                           verified_alpha=0.019) == "WATCHLIST"
        assert next_status("WATCHLIST", samples=500, orthogonal_win_rate=0.51,
                           verified_alpha=-0.01) == "WATCHLIST"

    def test_watchlist_alpha_promoted_source_does_not_insta_demote(self):
        # A source promoted via the alpha path (win_rate ≤ 0.50) must
        # NOT be demoted on the next tick just because its win rate
        # is sub-50%. The demotion rule requires BOTH paths to fail.
        assert next_status("WATCHLIST", samples=200, orthogonal_win_rate=0.41,
                           verified_alpha=0.007) == "WATCHLIST"

    def test_watchlist_demotes_when_both_paths_fail(self):
        # Sub-50% win rate AND sub-50-bps alpha → demote.
        assert next_status("WATCHLIST", samples=200, orthogonal_win_rate=0.40,
                           verified_alpha=0.001) == "UNTRUSTED"
        # Negative alpha AND sub-50% win rate → demote.
        assert next_status("WATCHLIST", samples=100, orthogonal_win_rate=0.40,
                           verified_alpha=-0.02) == "UNTRUSTED"

    def test_watchlist_stays_when_only_winrate_fails(self):
        # Legit alpha keeps it on WATCHLIST even at sub-50% win rate.
        assert next_status("WATCHLIST", samples=500, orthogonal_win_rate=0.30,
                           verified_alpha=0.008) == "WATCHLIST"

    def test_watchlist_stays_when_only_alpha_fails(self):
        # Good win rate keeps it on WATCHLIST even with weak alpha.
        assert next_status("WATCHLIST", samples=500, orthogonal_win_rate=0.55,
                           verified_alpha=0.001) == "WATCHLIST"

    def test_trusted_demotes_to_watchlist_on_negative_alpha(self):
        assert next_status("TRUSTED", samples=300, orthogonal_win_rate=0.60,
                           verified_alpha=-0.001) == "WATCHLIST"

    def test_trusted_stays_trusted_on_positive_alpha(self):
        assert next_status("TRUSTED", samples=300, orthogonal_win_rate=0.60,
                           verified_alpha=0.05) == "TRUSTED"

    def test_unknown_status_is_not_touched(self):
        # Verifier refuses to invent new phase names.
        assert next_status("QUARANTINE", samples=1000, orthogonal_win_rate=1.0,
                           verified_alpha=1.0) == "QUARANTINE"


# ─────────────────────────── Integration path (async) ───────────────────────────


class TestResolveSourceIntegration:
    """Integration tests using a fake price fetcher.

    These exercise the full DB path (find, update, upsert) against
    the test Mongo. Guard: only runs if the test DB is reachable.
    Tests seed rows themselves and clean up on teardown so they're
    order-independent.
    """

    @pytest.mark.asyncio
    async def test_fresh_source_stays_untrusted_below_threshold(self):
        from datetime import datetime, timezone, timedelta
        from db import db
        from namespaces import EXTERNAL_SIGNALS, EXTERNAL_SOURCE_CREDIBILITY
        from verifier.witness_resolver import resolve_source

        # Seed 10 witness rows — below the 50-sample floor.
        now = datetime.now(timezone.utc)
        old_ts = (now - timedelta(hours=48)).isoformat()
        await db[EXTERNAL_SIGNALS].delete_many({"source": "test_polygon_fresh"})
        await db[EXTERNAL_SOURCE_CREDIBILITY].delete_many({"source": "test_polygon_fresh"})
        for i in range(10):
            await db[EXTERNAL_SIGNALS].insert_one({
                "id": f"row-{i}",
                "source": "test_polygon_fresh",
                "symbol": "NVDA",
                "side": "BUY",
                "bar_close_ts": old_ts,
                "verifier_status": "UNTRUSTED",
                "influence_allowed": False,
            })

        async def fake_prices(symbol, ts):
            # BUY was right — price went from 100 → 105 (+500 bps)
            return 100.0 if ts == old_ts else 105.0

        summary = await resolve_source(
            "test_polygon_fresh", fake_prices, now=now,
        )
        assert summary.rows_resolved == 10
        assert summary.aggregate_after["samples"] == 10
        assert summary.aggregate_after["wins"] == 10  # all BUY, +500bps > 50bps
        assert summary.status_after == "UNTRUSTED"  # samples < 50
        assert summary.status_changed is False

        await db[EXTERNAL_SIGNALS].delete_many({"source": "test_polygon_fresh"})
        await db[EXTERNAL_SOURCE_CREDIBILITY].delete_many({"source": "test_polygon_fresh"})

    @pytest.mark.asyncio
    async def test_source_at_threshold_promotes_and_flips_influence(self):
        from datetime import datetime, timezone, timedelta
        from db import db
        from namespaces import EXTERNAL_SIGNALS, EXTERNAL_SOURCE_CREDIBILITY
        from verifier.witness_resolver import resolve_source

        # Seed 60 rows — over the 50-sample floor. All winning BUYs.
        now = datetime.now(timezone.utc)
        old_ts = (now - timedelta(hours=48)).isoformat()
        await db[EXTERNAL_SIGNALS].delete_many({"source": "test_polygon_promote"})
        await db[EXTERNAL_SOURCE_CREDIBILITY].delete_many({"source": "test_polygon_promote"})
        for i in range(60):
            await db[EXTERNAL_SIGNALS].insert_one({
                "id": f"promo-{i}",
                "source": "test_polygon_promote",
                "symbol": "NVDA",
                "side": "BUY",
                "bar_close_ts": old_ts,
                "verifier_status": "UNTRUSTED",
                "influence_allowed": False,
            })

        async def fake_prices(symbol, ts):
            return 100.0 if ts == old_ts else 105.0

        summary = await resolve_source(
            "test_polygon_promote", fake_prices, now=now,
        )
        assert summary.rows_resolved == 60
        assert summary.status_after == "WATCHLIST"
        assert summary.status_changed is True

        # Verify all rows had influence_allowed flipped to True on promotion.
        remaining_hostile = await db[EXTERNAL_SIGNALS].count_documents({
            "source": "test_polygon_promote", "influence_allowed": False,
        })
        assert remaining_hostile == 0

        await db[EXTERNAL_SIGNALS].delete_many({"source": "test_polygon_promote"})
        await db[EXTERNAL_SOURCE_CREDIBILITY].delete_many({"source": "test_polygon_promote"})

    @pytest.mark.asyncio
    async def test_recent_rows_are_skipped(self):
        from datetime import datetime, timezone, timedelta
        from db import db
        from namespaces import EXTERNAL_SIGNALS, EXTERNAL_SOURCE_CREDIBILITY
        from verifier.witness_resolver import resolve_source

        now = datetime.now(timezone.utc)
        # Row that's only 1h old — not yet horizon-eligible.
        recent_ts = (now - timedelta(hours=1)).isoformat()
        await db[EXTERNAL_SIGNALS].delete_many({"source": "test_polygon_recent"})
        await db[EXTERNAL_SOURCE_CREDIBILITY].delete_many({"source": "test_polygon_recent"})
        await db[EXTERNAL_SIGNALS].insert_one({
            "id": "recent-1",
            "source": "test_polygon_recent",
            "symbol": "NVDA",
            "side": "BUY",
            "bar_close_ts": recent_ts,
        })

        async def fake_prices(symbol, ts):
            return 100.0

        summary = await resolve_source(
            "test_polygon_recent", fake_prices, now=now,
        )
        assert summary.rows_resolved == 0
        assert summary.rows_skipped_too_recent == 1

        await db[EXTERNAL_SIGNALS].delete_many({"source": "test_polygon_recent"})
        await db[EXTERNAL_SOURCE_CREDIBILITY].delete_many({"source": "test_polygon_recent"})

    @pytest.mark.asyncio
    async def test_missing_prices_are_skipped_not_counted(self):
        from datetime import datetime, timezone, timedelta
        from db import db
        from namespaces import EXTERNAL_SIGNALS, EXTERNAL_SOURCE_CREDIBILITY
        from verifier.witness_resolver import resolve_source

        now = datetime.now(timezone.utc)
        old_ts = (now - timedelta(hours=48)).isoformat()
        await db[EXTERNAL_SIGNALS].delete_many({"source": "test_polygon_noprice"})
        await db[EXTERNAL_SOURCE_CREDIBILITY].delete_many({"source": "test_polygon_noprice"})
        await db[EXTERNAL_SIGNALS].insert_one({
            "id": "noprice-1",
            "source": "test_polygon_noprice",
            "symbol": "OBSCURE",
            "side": "BUY",
            "bar_close_ts": old_ts,
        })

        async def fake_prices(symbol, ts):
            return None  # simulate price feed unable to resolve

        summary = await resolve_source(
            "test_polygon_noprice", fake_prices, now=now,
        )
        assert summary.rows_resolved == 0
        assert summary.rows_skipped_price_missing == 1
        # Ledger should NOT have any samples counted from unresolved rows.
        assert summary.aggregate_after["samples"] == 0

        await db[EXTERNAL_SIGNALS].delete_many({"source": "test_polygon_noprice"})
        await db[EXTERNAL_SOURCE_CREDIBILITY].delete_many({"source": "test_polygon_noprice"})

    @pytest.mark.asyncio
    async def test_idempotent_second_run_finds_no_new_rows(self):
        from datetime import datetime, timezone, timedelta
        from db import db
        from namespaces import EXTERNAL_SIGNALS, EXTERNAL_SOURCE_CREDIBILITY
        from verifier.witness_resolver import resolve_source

        now = datetime.now(timezone.utc)
        old_ts = (now - timedelta(hours=48)).isoformat()
        await db[EXTERNAL_SIGNALS].delete_many({"source": "test_polygon_idem"})
        await db[EXTERNAL_SOURCE_CREDIBILITY].delete_many({"source": "test_polygon_idem"})
        for i in range(3):
            await db[EXTERNAL_SIGNALS].insert_one({
                "id": f"idem-{i}",
                "source": "test_polygon_idem",
                "symbol": "NVDA",
                "side": "BUY",
                "bar_close_ts": old_ts,
            })

        async def fake_prices(symbol, ts):
            return 100.0 if ts == old_ts else 105.0

        first = await resolve_source("test_polygon_idem", fake_prices, now=now)
        second = await resolve_source("test_polygon_idem", fake_prices, now=now)

        assert first.rows_resolved == 3
        # Second pass: all rows already have resolution_outcome — skipped.
        assert second.rows_examined == 0
        assert second.rows_resolved == 0
        # Ledger unchanged between runs.
        assert first.aggregate_after == second.aggregate_after

        await db[EXTERNAL_SIGNALS].delete_many({"source": "test_polygon_idem"})
        await db[EXTERNAL_SOURCE_CREDIBILITY].delete_many({"source": "test_polygon_idem"})
