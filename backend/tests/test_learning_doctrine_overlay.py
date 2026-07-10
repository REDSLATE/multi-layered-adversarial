"""Bounded doctrine-overlay engine — tests.

Locks in the operator's ±0.20 clamp contract, the exact-bucket-dim
lookup, TTL cache behaviour, and safe defaults on missing / bad data.

Doctrine:
    * No approved lesson → multiplier == 1.0, delta == 0.0
    * Approved edge lesson → +0.10 default (up to +0.20 clamp)
    * Approved bleed lesson → -0.10 default (down to -0.20 clamp)
    * Explicit `modifier` field on the lesson doc overrides default
    * Explicit modifier still clamped to ±0.20
    * Partial / malformed dims → 1.0 / 0.0 (fail closed)
"""
from __future__ import annotations

import sys

import pytest

sys.path.insert(0, "/app/backend")

from db import db  # noqa: E402
from shared.learning.bucket_analyzer import _bucket_key  # noqa: E402
from shared.learning import doctrine_overlay  # noqa: E402
from shared.learning.doctrine_overlay import (  # noqa: E402
    DEFAULT_BLEED_MODIFIER,
    DEFAULT_EDGE_MODIFIER,
    MODIFIER_MAX,
    MODIFIER_MIN,
    MULTIPLIER_CENTRE,
    MULTIPLIER_MAX,
    MULTIPLIER_MIN,
    get_notional_multiplier,
    get_threshold_delta,
    reload_overlay_cache_for_tests,
)
from shared.learning.lesson_proposer import LEARNING_LESSONS  # noqa: E402


_TEST_PREFIX = "overlay-test-"

_DIMS = {
    "lane": "equity",
    "action": "BUY",
    "notional_source": "micro_default",
    "rvol_band": "high",
    "spread_band": "tight",
    "doctrine_band": "clean",
}


def _bid_for(dims):
    bid, _ = _bucket_key(**{k: dims[k] for k in (
        "lane", "action", "notional_source",
        "rvol_band", "spread_band", "doctrine_band",
    )})
    return bid


@pytest.fixture(autouse=True)
async def _purge_lessons():
    """Purge synthetic lesson rows and reset the TTL cache before + after."""
    async def _wipe():
        await db[LEARNING_LESSONS].delete_many(
            {"bucket_label": {"$regex": f"^{_TEST_PREFIX}"}},
        )
        await db[LEARNING_LESSONS].delete_many(
            {"bucket_id": _bid_for(_DIMS)},
        )
        reload_overlay_cache_for_tests()
    await _wipe()
    yield
    await _wipe()


# ─── constants sanity ─────────────────────────────────────────────


def test_hard_clamp_constants_are_20_pct():
    assert MODIFIER_MIN == -0.20
    assert MODIFIER_MAX == 0.20
    assert MULTIPLIER_MIN == pytest.approx(0.80)
    assert MULTIPLIER_MAX == pytest.approx(1.20)
    assert MULTIPLIER_CENTRE == 1.0


def test_default_modifiers_within_clamp():
    """Defaults must obey the ±0.20 clamp — a change to defaults
    that violates the clamp is a config regression."""
    assert MODIFIER_MIN <= DEFAULT_EDGE_MODIFIER <= MODIFIER_MAX
    assert MODIFIER_MIN <= DEFAULT_BLEED_MODIFIER <= MODIFIER_MAX


# ─── no-lesson defaults ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_multiplier_defaults_to_one_when_no_approved_lesson():
    mult = await get_notional_multiplier(_DIMS, db=db)
    assert mult == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_delta_defaults_to_zero_when_no_approved_lesson():
    delta = await get_threshold_delta(_DIMS, db=db)
    assert delta == pytest.approx(0.0)


# ─── unapproved lessons DO NOT apply ──────────────────────────────


@pytest.mark.asyncio
async def test_proposed_lesson_does_not_apply():
    """Only `state=approved` lessons feed the cache. A `state=proposed`
    lesson must be invisible to the overlay engine."""
    await db[LEARNING_LESSONS].insert_one({
        "_id": f"{_TEST_PREFIX}proposed-1",
        "bucket_id": _bid_for(_DIMS),
        "bucket_label": f"{_TEST_PREFIX}proposed-label",
        "kind": "edge",
        "state": "proposed",
    })
    reload_overlay_cache_for_tests()
    mult = await get_notional_multiplier(_DIMS, db=db)
    assert mult == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_rejected_lesson_does_not_apply():
    await db[LEARNING_LESSONS].insert_one({
        "_id": f"{_TEST_PREFIX}rejected-1",
        "bucket_id": _bid_for(_DIMS),
        "bucket_label": f"{_TEST_PREFIX}rejected-label",
        "kind": "edge",
        "state": "rejected",
    })
    reload_overlay_cache_for_tests()
    mult = await get_notional_multiplier(_DIMS, db=db)
    assert mult == pytest.approx(1.0)


# ─── approved edge lesson applies default +0.10 ───────────────────


@pytest.mark.asyncio
async def test_approved_edge_lesson_yields_default_positive_modifier():
    """An approved edge lesson with no explicit `modifier` → default
    +0.10 → multiplier 1.10, delta +0.10."""
    await db[LEARNING_LESSONS].insert_one({
        "_id": f"{_TEST_PREFIX}edge-1",
        "bucket_id": _bid_for(_DIMS),
        "bucket_label": f"{_TEST_PREFIX}edge-label",
        "kind": "edge",
        "state": "approved",
    })
    reload_overlay_cache_for_tests()
    mult = await get_notional_multiplier(_DIMS, db=db)
    delta = await get_threshold_delta(_DIMS, db=db)
    assert mult == pytest.approx(1.10)
    assert delta == pytest.approx(0.10)


# ─── approved bleed lesson applies default -0.10 ─────────────────


@pytest.mark.asyncio
async def test_approved_bleed_lesson_yields_default_negative_modifier():
    await db[LEARNING_LESSONS].insert_one({
        "_id": f"{_TEST_PREFIX}bleed-1",
        "bucket_id": _bid_for(_DIMS),
        "bucket_label": f"{_TEST_PREFIX}bleed-label",
        "kind": "bleed",
        "state": "approved",
    })
    reload_overlay_cache_for_tests()
    mult = await get_notional_multiplier(_DIMS, db=db)
    delta = await get_threshold_delta(_DIMS, db=db)
    assert mult == pytest.approx(0.90)
    assert delta == pytest.approx(-0.10)


# ─── explicit modifier overrides default ──────────────────────────


@pytest.mark.asyncio
async def test_explicit_modifier_overrides_default():
    await db[LEARNING_LESSONS].insert_one({
        "_id": f"{_TEST_PREFIX}explicit-1",
        "bucket_id": _bid_for(_DIMS),
        "bucket_label": f"{_TEST_PREFIX}explicit-label",
        "kind": "edge",
        "state": "approved",
        "modifier": 0.15,
    })
    reload_overlay_cache_for_tests()
    mult = await get_notional_multiplier(_DIMS, db=db)
    assert mult == pytest.approx(1.15)


# ─── ±0.20 hard clamp cannot be bypassed ─────────────────────────


@pytest.mark.asyncio
async def test_modifier_above_clamp_gets_capped_to_positive_20():
    """An approved lesson with `modifier=0.50` (runaway config) must
    STILL be clamped to +0.20 by the overlay engine."""
    await db[LEARNING_LESSONS].insert_one({
        "_id": f"{_TEST_PREFIX}clamp-hi-1",
        "bucket_id": _bid_for(_DIMS),
        "bucket_label": f"{_TEST_PREFIX}clamp-hi-label",
        "kind": "edge",
        "state": "approved",
        "modifier": 0.50,
    })
    reload_overlay_cache_for_tests()
    mult = await get_notional_multiplier(_DIMS, db=db)
    delta = await get_threshold_delta(_DIMS, db=db)
    assert mult == pytest.approx(MULTIPLIER_MAX)  # 1.20
    assert delta == pytest.approx(MODIFIER_MAX)   # +0.20


@pytest.mark.asyncio
async def test_modifier_below_clamp_gets_capped_to_negative_20():
    await db[LEARNING_LESSONS].insert_one({
        "_id": f"{_TEST_PREFIX}clamp-lo-1",
        "bucket_id": _bid_for(_DIMS),
        "bucket_label": f"{_TEST_PREFIX}clamp-lo-label",
        "kind": "bleed",
        "state": "approved",
        "modifier": -0.75,
    })
    reload_overlay_cache_for_tests()
    mult = await get_notional_multiplier(_DIMS, db=db)
    delta = await get_threshold_delta(_DIMS, db=db)
    assert mult == pytest.approx(MULTIPLIER_MIN)  # 0.80
    assert delta == pytest.approx(MODIFIER_MIN)   # -0.20


# ─── exact bucket-dim match required ─────────────────────────────


@pytest.mark.asyncio
async def test_dims_mismatch_does_not_apply_lesson():
    """An approved lesson on `(equity, BUY, …)` must NOT leak into a
    `(crypto, BUY, …)` intent even if every other dim matches."""
    await db[LEARNING_LESSONS].insert_one({
        "_id": f"{_TEST_PREFIX}mismatch-1",
        "bucket_id": _bid_for(_DIMS),  # equity dims
        "bucket_label": f"{_TEST_PREFIX}mismatch-label",
        "kind": "edge",
        "state": "approved",
    })
    reload_overlay_cache_for_tests()

    crypto_dims = dict(_DIMS)
    crypto_dims["lane"] = "crypto"
    mult = await get_notional_multiplier(crypto_dims, db=db)
    assert mult == pytest.approx(1.0), (
        "Overlay leaked across bucket-dim boundaries — exact-match violated"
    )


@pytest.mark.asyncio
async def test_missing_dim_key_fails_closed_to_no_op():
    """A dim vector missing `lane` cannot be looked up safely →
    return 1.0 / 0.0 (no change) rather than guessing."""
    partial = dict(_DIMS)
    partial.pop("lane")
    mult = await get_notional_multiplier(partial, db=db)
    delta = await get_threshold_delta(partial, db=db)
    assert mult == pytest.approx(1.0)
    assert delta == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_none_dims_fails_closed_to_no_op():
    mult = await get_notional_multiplier(None, db=db)
    delta = await get_threshold_delta(None, db=db)
    assert mult == pytest.approx(1.0)
    assert delta == pytest.approx(0.0)


# ─── TTL cache behaviour ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_cache_holds_across_calls():
    """Second lookup for the same dims must NOT re-query Mongo —
    cache should be a single-load until TTL expires."""
    await db[LEARNING_LESSONS].insert_one({
        "_id": f"{_TEST_PREFIX}cache-1",
        "bucket_id": _bid_for(_DIMS),
        "bucket_label": f"{_TEST_PREFIX}cache-label",
        "kind": "edge",
        "state": "approved",
    })
    reload_overlay_cache_for_tests()
    m1 = await get_notional_multiplier(_DIMS, db=db)
    assert m1 == pytest.approx(1.10)

    # Delete the lesson. If the cache were hit-per-call, the next
    # lookup would see 1.0. Since we're inside TTL, we still get 1.10.
    await db[LEARNING_LESSONS].delete_one({"_id": f"{_TEST_PREFIX}cache-1"})
    m2 = await get_notional_multiplier(_DIMS, db=db)
    assert m2 == pytest.approx(1.10), (
        "TTL cache broken — approvals should stick for CACHE_TTL_SEC"
    )


@pytest.mark.asyncio
async def test_cache_reload_after_reset():
    """Force-reload via the test hook picks up new approvals."""
    reload_overlay_cache_for_tests()
    m1 = await get_notional_multiplier(_DIMS, db=db)
    assert m1 == pytest.approx(1.0)

    await db[LEARNING_LESSONS].insert_one({
        "_id": f"{_TEST_PREFIX}reload-1",
        "bucket_id": _bid_for(_DIMS),
        "bucket_label": f"{_TEST_PREFIX}reload-label",
        "kind": "edge",
        "state": "approved",
    })
    reload_overlay_cache_for_tests()
    m2 = await get_notional_multiplier(_DIMS, db=db)
    assert m2 == pytest.approx(1.10)


# ─── the two outputs are the intended shape (0.80..1.20 vs ±0.20) ─


@pytest.mark.asyncio
async def test_notional_multiplier_is_centred_at_one():
    """The MULTIPLIER function returns a value AROUND 1.0. Callers
    must be able to `notional = base * multiplier` safely. This test
    guards against a future refactor that accidentally returns the
    raw signed modifier from `get_notional_multiplier`."""
    mult = await get_notional_multiplier(_DIMS, db=db)
    assert MULTIPLIER_MIN <= mult <= MULTIPLIER_MAX
    assert mult >= 0.80  # would fail if the raw ±0.20 modifier leaked


@pytest.mark.asyncio
async def test_threshold_delta_is_signed_around_zero():
    """The THRESHOLD function returns a SIGNED delta AROUND 0.0.
    Callers do `threshold = base + delta`."""
    delta = await get_threshold_delta(_DIMS, db=db)
    assert MODIFIER_MIN <= delta <= MODIFIER_MAX
