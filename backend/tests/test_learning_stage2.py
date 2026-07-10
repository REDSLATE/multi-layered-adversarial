"""Stage 2 tests — Bucket Analyzer + Wilson guard + Lesson Proposer.

Locks in the operator's guardrails (2026-07-09 iter-22):

    Lesson proposal requires:
        samples >= 30
        edge:   avg_5m_bps > 0    AND wilson_lower >= 0.50
        bleed:  avg_5m_bps < -10

Anything else stays quiet. Prevents early noise from becoming
doctrine.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, "/app/backend")

from db import db  # noqa: E402
from shared.learning.bucket_analyzer import (  # noqa: E402
    LEARNING_BUCKETS, _bucket_key, _doctrine_band,
    rebuild_buckets, wilson_lower_bound,
)
from shared.learning.lesson_proposer import (  # noqa: E402
    LEARNING_LESSONS, MIN_SAMPLE_SIZE, propose_lessons,
)
from shared.learning.live_loop import LEARNING_EXPERIENCES  # noqa: E402


_TEST_PREFIX = "stage2-test-"


@pytest.fixture(autouse=True)
async def _purge():
    """Purge every synthetic bucket/lesson/experience row before + after."""
    async def _wipe():
        await db[LEARNING_EXPERIENCES].delete_many(
            {"intent_id": {"$regex": f"^{_TEST_PREFIX}"}},
        )
        # Buckets and lessons keyed by hash of test dims — clear all
        # rows whose label contains our synthetic markers.
        await db[LEARNING_BUCKETS].delete_many(
            {"label": {"$regex": "stage2test"}},
        )
        await db[LEARNING_LESSONS].delete_many(
            {"bucket_label": {"$regex": "stage2test"}},
        )
    await _wipe()
    yield
    await _wipe()


# ─── Wilson math ────────────────────────────────────────────────

def test_wilson_zero_samples_is_zero():
    assert wilson_lower_bound(0, 0) == 0.0


def test_wilson_perfect_run_short_boosts_confidence_but_min_samples_blocks():
    """5/5 wins gives Wilson-lower ≈ 0.57 — clears the 0.50 floor
    alone. This is why the MIN_SAMPLE_SIZE=30 guardrail is essential:
    together the two gates block a lucky-streak-becoming-doctrine
    scenario. Test: Wilson passes, but min_samples must also gate."""
    lb = wilson_lower_bound(5, 5)
    # Wilson alone is not sufficient — even a lucky 5/5 clears 0.50.
    assert lb >= 0.50
    # The sample-size gate is what actually blocks it downstream —
    # 5 << MIN_SAMPLE_SIZE (30). Tested in the pipeline tests below.


def test_wilson_100_percent_of_3_below_50():
    """3/3 wins is truly not enough — Wilson-lower stays below 0.50.
    Confirms Wilson handles the smallest cases correctly."""
    assert wilson_lower_bound(3, 3) < 0.50


def test_wilson_large_sample_clears_when_hit_rate_high():
    """30 wins out of 40 (75%) clears the 0.50 floor."""
    lb = wilson_lower_bound(30, 40)
    assert lb >= 0.50


def test_wilson_never_negative():
    """Even a 0/100 result must return >= 0.0 (no negative rates)."""
    assert wilson_lower_bound(0, 100) >= 0.0


# ─── Bucketing ──────────────────────────────────────────────────

def test_bucket_key_stable_and_short():
    """Same dims → same 16-char id. Different dims → different id."""
    a, _ = _bucket_key(
        lane="equity", action="BUY", notional_source="micro_default",
        rvol_band="normal", spread_band="tight", doctrine_band="clean",
    )
    b, _ = _bucket_key(
        lane="equity", action="BUY", notional_source="micro_default",
        rvol_band="normal", spread_band="tight", doctrine_band="clean",
    )
    c, _ = _bucket_key(
        lane="crypto", action="BUY", notional_source="micro_default",
        rvol_band="normal", spread_band="tight", doctrine_band="clean",
    )
    assert a == b
    assert a != c
    assert len(a) == 16


def test_doctrine_band_marginal_triple_recognized():
    """The exact {liquidity_ok, quality_ok, score_ok} triple maps to
    the `marginal_3` band — that's the operator's marginal-setup
    fingerprint the assign_micro_notional rule keys off."""
    assert _doctrine_band({
        "seats": {"execution_judge": {
            "failed_checks": ["liquidity_ok", "quality_ok", "score_ok"],
        }},
    }) == "marginal_3"


def test_doctrine_band_clean_when_no_failed_checks():
    assert _doctrine_band({
        "seats": {"execution_judge": {"failed_checks": []}},
    }) == "clean"


def test_doctrine_band_missing_packet():
    assert _doctrine_band(None) == "no_packet"
    assert _doctrine_band({}) == "no_packet"


# ─── End-to-end bucket rebuild + lesson proposal ───────────────

async def _seed_experience(
    intent_id: str, *, win: bool, bps_5m: float, samples_tag: str = "A",
):
    """Insert a resolved experience. `samples_tag` lets us push
    multiple docs into the same bucket by holding all bucket dims
    constant."""
    await db[LEARNING_EXPERIENCES].insert_one({
        "intent_id": intent_id,
        "symbol": f"stage2test-{samples_tag}",
        "lane": "equity",
        "action": "BUY",
        "notional_source": "stage2test",
        "notional_usd": 5.0,
        "entry_price": 100.0, "fill_price": 100.0,
        "terminal_state": "submitted",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "outcome_5m_bps": bps_5m,
        "outcome_15m_bps": bps_5m,
        "outcome_1h_bps": bps_5m,
        "win": win,
        "features": {"rvol": 1.0, "spread_bps": 15.0},
        "doctrine": {"seats": {"execution_judge": {"failed_checks": []}}},
    })


@pytest.mark.asyncio
async def test_undersampled_bucket_yields_no_lesson():
    """A bucket with 10 wins/10 samples (100% hit rate) but below
    MIN_SAMPLE_SIZE must NOT emit any lesson. This is the exact case
    the operator called out — no doctrine noise from tiny buckets."""
    for i in range(10):
        await _seed_experience(
            f"{_TEST_PREFIX}under-{i}", win=True, bps_5m=200.0,
        )
    b_counts = await rebuild_buckets(db)
    l_counts = await propose_lessons(db)

    assert b_counts["experiences_scanned"] >= 10
    # No lesson should have been proposed for our synthetic bucket.
    lesson = await db[LEARNING_LESSONS].find_one(
        {"bucket_label": {"$regex": "stage2test"}},
    )
    assert lesson is None
    assert l_counts["skipped_undersample"] >= 1


@pytest.mark.asyncio
async def test_edge_bucket_with_enough_samples_yields_edge_lesson():
    """35 samples, 28 wins (80% hit rate) → Wilson-lower ≥ 0.50
    AND avg_bps > 0 → EDGE lesson emitted, state='proposed'."""
    for i in range(28):
        await _seed_experience(
            f"{_TEST_PREFIX}edge-w-{i}", win=True, bps_5m=250.0,
        )
    for i in range(7):
        await _seed_experience(
            f"{_TEST_PREFIX}edge-l-{i}", win=False, bps_5m=-100.0,
        )
    await rebuild_buckets(db)
    l_counts = await propose_lessons(db)

    assert l_counts["edge_lessons"] >= 1
    lesson = await db[LEARNING_LESSONS].find_one(
        {"bucket_label": {"$regex": "stage2test"}, "kind": "edge"},
    )
    assert lesson is not None
    assert lesson["state"] == "proposed", (
        "Lessons must land as 'proposed' only — never auto-applied"
    )
    ev = lesson["evidence"]
    assert ev["samples"] >= MIN_SAMPLE_SIZE
    assert ev["wilson_lower"] >= 0.50
    assert ev["avg_5m_bps"] > 0


@pytest.mark.asyncio
async def test_bleed_bucket_yields_bleed_lesson():
    """30+ samples with avg_5m_bps < -10 → BLEED lesson emitted."""
    # 30 samples all at -15 bps → avg = -15, well below -10.
    for i in range(30):
        await _seed_experience(
            f"{_TEST_PREFIX}bleed-{i}", win=False, bps_5m=-15.0,
        )
    await rebuild_buckets(db)
    l_counts = await propose_lessons(db)

    assert l_counts["bleed_lessons"] >= 1
    lesson = await db[LEARNING_LESSONS].find_one(
        {"bucket_label": {"$regex": "stage2test"}, "kind": "bleed"},
    )
    assert lesson is not None
    assert lesson["state"] == "proposed"
    assert lesson["evidence"]["avg_5m_bps"] < -10


@pytest.mark.asyncio
async def test_noise_bucket_no_lesson():
    """Enough samples but avg ~ -3 bps (noise, above -10 threshold)
    and hit-rate ~ 50% → neither edge nor bleed."""
    for i in range(15):
        await _seed_experience(
            f"{_TEST_PREFIX}noise-w-{i}", win=True, bps_5m=3.0,
        )
    for i in range(20):
        await _seed_experience(
            f"{_TEST_PREFIX}noise-l-{i}", win=False, bps_5m=-8.0,
        )
    await rebuild_buckets(db)
    l_counts = await propose_lessons(db)

    lesson = await db[LEARNING_LESSONS].find_one(
        {"bucket_label": {"$regex": "stage2test"}},
    )
    assert lesson is None, "Noise bucket emitted a lesson — guardrails failed"
    assert l_counts["skipped_noise"] >= 1


@pytest.mark.asyncio
async def test_approved_lesson_survives_re_proposal():
    """An already-approved lesson keeps its `approved` state when
    re-proposed with fresh evidence — `$setOnInsert` protects the
    state field."""
    for i in range(35):
        await _seed_experience(
            f"{_TEST_PREFIX}approve-{i}", win=True, bps_5m=250.0,
        )
    await rebuild_buckets(db)
    await propose_lessons(db)
    lesson = await db[LEARNING_LESSONS].find_one(
        {"bucket_label": {"$regex": "stage2test"}, "kind": "edge"},
    )
    assert lesson is not None
    # Simulate operator approval.
    await db[LEARNING_LESSONS].update_one(
        {"_id": lesson["_id"]},
        {"$set": {"state": "approved", "approved_by": "test-op"}},
    )
    # Re-run proposer — evidence updates, state must NOT reset.
    await propose_lessons(db)
    still = await db[LEARNING_LESSONS].find_one({"_id": lesson["_id"]})
    assert still["state"] == "approved", (
        "Re-proposal reset an approved lesson back to 'proposed' — "
        "$setOnInsert protection violated"
    )


# ─── Shrunk-EV floor (2026-02-19 operator directive) ───────────

def test_shrunk_ev_zero_samples_returns_zero():
    from shared.learning.lesson_proposer import _shrunk_ev_bps
    assert _shrunk_ev_bps(100.0, 0) == 0.0


def test_shrunk_ev_pulls_small_samples_toward_zero():
    """30 samples averaging +20 bps → shrunk = 20 * 30/130 ≈ 4.6.
    Below the 5.0 floor by design — small-sample flukes must die."""
    from shared.learning.lesson_proposer import (
        _shrunk_ev_bps, SHRINKAGE_CONSTANT,
    )
    assert SHRINKAGE_CONSTANT == 100.0
    shrunk = _shrunk_ev_bps(20.0, 30)
    assert shrunk == pytest.approx(20.0 * 30 / 130.0, rel=1e-6)
    assert shrunk < 5.0


def test_shrunk_ev_large_samples_survive():
    """300 samples averaging +20 bps → shrunk = 20 * 300/400 = 15
    bps. Well above the 5.0 floor — real edge is preserved."""
    from shared.learning.lesson_proposer import _shrunk_ev_bps
    shrunk = _shrunk_ev_bps(20.0, 300)
    assert shrunk == pytest.approx(15.0)


@pytest.mark.asyncio
async def test_edge_bucket_with_small_shrunk_ev_yields_no_lesson():
    """30 samples, all wins, avg +20 bps → Wilson clears (30/30 →
    ~0.88), sample-size clears (30 >= 30), BUT shrunk EV = 20*30/130
    = 4.6 < 5.0 floor. NO lesson must be emitted — the shrinkage
    guard is what kills the small-sample fluke."""
    for i in range(30):
        await _seed_experience(
            f"{_TEST_PREFIX}shrink-{i}", win=True, bps_5m=20.0,
        )
    await rebuild_buckets(db)
    l_counts = await propose_lessons(db)

    # NOT recorded as edge, must be caught by the noise skip.
    assert l_counts.get("edge_lessons", 0) == 0
    assert l_counts["skipped_noise"] >= 1
    lesson = await db[LEARNING_LESSONS].find_one(
        {"bucket_label": {"$regex": "stage2test"}, "kind": "edge"},
    )
    assert lesson is None, (
        "Shrunk-EV guard failed — small-sample fluke emitted an edge lesson"
    )


@pytest.mark.asyncio
async def test_edge_lesson_evidence_includes_shrunk_ev_bps():
    """A lesson that DOES land must stamp `shrunk_ev_bps` in evidence
    so Kernel review sees the shrunk (not just raw) number."""
    # 35 samples, avg 250 bps → shrunk = 250*35/135 ≈ 64.8 bps.
    for i in range(28):
        await _seed_experience(
            f"{_TEST_PREFIX}ev-w-{i}", win=True, bps_5m=250.0,
        )
    for i in range(7):
        await _seed_experience(
            f"{_TEST_PREFIX}ev-l-{i}", win=False, bps_5m=250.0,
        )
    await rebuild_buckets(db)
    await propose_lessons(db)
    lesson = await db[LEARNING_LESSONS].find_one(
        {"bucket_label": {"$regex": "stage2test"}, "kind": "edge"},
    )
    assert lesson is not None
    ev = lesson["evidence"]
    assert "shrunk_ev_bps" in ev
    assert ev["shrunk_ev_bps"] >= 5.0
    # Sanity: shrunk should be LESS than raw avg (shrinkage direction).
    assert ev["shrunk_ev_bps"] < ev["avg_5m_bps"]
