"""Arbiter integration tests — 4-brain competition, DISARMED vs
LIVE emission, disagreement multiplier, cold-start neutrality.

These tests hit the real local MongoDB. Each test uses a unique
seat_key so runs don't collide.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

import pytest

from db import db
from mc_arbiter.arbiter import (
    BRM,
    MC_SEATS,
    STACK_ID,
    arbitrate,
    get_runtime_mode,
    load_dawe,
    save_dawe,
    set_runtime_mode,
    submit_opinion,
)
from mc_arbiter.models import DaweState, Direction, ModelOpinion, RuntimeMode
from mc_arbiter.seat_key import build_seat_key


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@pytest.fixture
def unique_seat():
    """Unique seat_key per test — includes a timestamp so parallel
    runs don't collide either."""
    lane = "equity"
    symbol = f"TEST{int(time.time() * 1000) % 100000}"
    key = build_seat_key(lane, symbol)
    yield key, lane, symbol
    # Cleanup: wipe every row for this seat.
    import asyncio
    async def _cleanup():
        await db[MC_SEATS].delete_many({"seat_key": key})
        # Also wipe any test DAWE state for the (brains, lane) pairs used.
        await db[BRM].update_one(
            {"_id": STACK_ID},
            {"$unset": {
                "brains.__test_camino": "",
                "brains.__test_barracuda": "",
                "brains.__test_hellcat": "",
                "brains.__test_gto": "",
            }},
        )
    asyncio.get_event_loop().run_until_complete(_cleanup())


def _op(brain: str, seat_key: str, direction: Direction, **k):
    base = dict(
        edge=0.60,
        confidence=0.75,
        regime_fit=0.80,
        urgency=0.50,
        price_at_signal=100.0,
        ts=_now_iso(),
    )
    base.update(k)
    return ModelOpinion(
        brain=brain, seat_key=seat_key, direction=direction, **base,
    )


# ── submit_opinion is idempotent ─────────────────────────────────

@pytest.mark.asyncio
async def test_submit_opinion_upserts_by_seat_and_brain(unique_seat):
    seat_key, lane, symbol = unique_seat
    r1 = await submit_opinion(_op("__test_camino", seat_key, Direction.LONG))
    assert r1["ok"] is True
    # Re-submit same brain — should update, not duplicate.
    r2 = await submit_opinion(
        _op("__test_camino", seat_key, Direction.LONG, confidence=0.99),
    )
    assert r2["ok"] is True
    rows = await db[MC_SEATS].find({"seat_key": seat_key}).to_list(10)
    assert len(rows) == 1
    assert rows[0]["confidence"] == 0.99


# ── 4-brain LONG competition, arbiter picks highest adjusted_rank ─

@pytest.mark.asyncio
async def test_four_brain_long_competition_disarmed(unique_seat):
    seat_key, lane, symbol = unique_seat
    # Camino: strongest raw signal. Barracuda: weaker. Hellcat: FLAT (never wins).
    await submit_opinion(_op("__test_camino", seat_key, Direction.LONG, edge=0.80, confidence=0.85))
    await submit_opinion(_op("__test_barracuda", seat_key, Direction.LONG, edge=0.55))
    await submit_opinion(_op("__test_hellcat", seat_key, Direction.FLAT, edge=0.50))
    await submit_opinion(_op("__test_gto", seat_key, Direction.LONG, edge=0.60))

    decision = await arbitrate(seat_key, runtime_mode=RuntimeMode.DISARMED)
    assert decision["winner_brain"] == "__test_camino"
    assert decision["winner_direction"] == "LONG"
    # DISARMED must NOT emit an intent.
    assert decision["intent_id"] is None
    assert decision["runtime_mode"] == "DISARMED"
    # Field includes the FLAT for grading.
    directions_in_field = {r["direction"] for r in decision["field"]}
    assert directions_in_field == {"LONG", "FLAT"}


# ── DAWE weight actually changes the winner ──────────────────────

@pytest.mark.asyncio
async def test_dawe_weight_flips_the_winner(unique_seat):
    seat_key, lane, symbol = unique_seat
    # Barracuda has slightly weaker raw signal than Camino…
    await submit_opinion(_op("__test_camino", seat_key, Direction.LONG, edge=0.70))
    await submit_opinion(_op("__test_barracuda", seat_key, Direction.LONG, edge=0.65))

    # …but Barracuda has been on fire this session (well-graded).
    hot_state = DaweState(
        brain="__test_barracuda", lane=lane,
        session_weight=1.35, recent_weight=1.20, prior_weight=1.00,
        grades_used_session=50,
    )
    cool_state = DaweState(
        brain="__test_camino", lane=lane,
        session_weight=0.70, recent_weight=0.80, prior_weight=1.00,
        grades_used_session=50,
    )
    await save_dawe(hot_state)
    await save_dawe(cool_state)

    decision = await arbitrate(seat_key, runtime_mode=RuntimeMode.DISARMED)
    # DAWE weight upgraded Barracuda past Camino.
    assert decision["winner_brain"] == "__test_barracuda"
    # Size multiplier stays within the sanity clamp.
    assert 0.30 <= decision["size_multiplier"] <= 2.00


# ── Cold start means weight has no authority ─────────────────────

@pytest.mark.asyncio
async def test_cold_start_ignores_dawe_weight(unique_seat):
    seat_key, lane, symbol = unique_seat
    # Even though "hot" is set to 1.35, grades_used_session=0 →
    # effective_weight forced to 1.0. Raw rank decides.
    await submit_opinion(_op("__test_camino", seat_key, Direction.LONG, edge=0.70))
    await submit_opinion(_op("__test_barracuda", seat_key, Direction.LONG, edge=0.65))

    hot_but_thin = DaweState(
        brain="__test_barracuda", lane=lane,
        session_weight=1.35, recent_weight=1.35, prior_weight=1.35,
        grades_used_session=0,  # cold-start
    )
    await save_dawe(hot_but_thin)

    decision = await arbitrate(seat_key, runtime_mode=RuntimeMode.DISARMED)
    # Camino wins because DAWE couldn't move the arm — thin data.
    assert decision["winner_brain"] == "__test_camino"


# ── FLAT-only field yields no winner ─────────────────────────────

@pytest.mark.asyncio
async def test_all_flat_produces_no_winner(unique_seat):
    seat_key, lane, symbol = unique_seat
    await submit_opinion(_op("__test_camino", seat_key, Direction.FLAT))
    await submit_opinion(_op("__test_barracuda", seat_key, Direction.FLAT))

    decision = await arbitrate(seat_key, runtime_mode=RuntimeMode.DISARMED)
    assert decision["winner_brain"] is None
    assert decision["reason"] == "all_flat"
    assert decision["intent_id"] is None


# ── Empty seat surfaces cleanly ──────────────────────────────────

@pytest.mark.asyncio
async def test_arbitrate_empty_seat():
    # Never-populated seat_key. Should not raise.
    decision = await arbitrate(
        "equity:__NEVER__:2000-01-01T00:00:00Z",
        runtime_mode=RuntimeMode.DISARMED,
    )
    assert decision["winner_brain"] is None
    assert decision["reason"] == "no_opinions"


# ── Disagreement multiplier bites when opposition is strong ──────

@pytest.mark.asyncio
async def test_disagreement_multiplier_shrinks_size_when_split(unique_seat):
    seat_key, lane, symbol = unique_seat
    # Strong LONG.
    await submit_opinion(_op("__test_camino", seat_key, Direction.LONG, edge=0.80, confidence=0.85))
    # Strong opposing SHORT.
    await submit_opinion(_op("__test_barracuda", seat_key, Direction.SHORT, edge=0.75, confidence=0.80))

    decision = await arbitrate(seat_key, runtime_mode=RuntimeMode.DISARMED)
    # Winner is one of them (higher adjusted rank wins).
    assert decision["winner_brain"] in {"__test_camino", "__test_barracuda"}
    # Disagreement multiplier is below 1.0 because opposition_strength > 0.
    assert decision["disagreement_multiplier"] < 1.00
    assert decision["disagreement_multiplier"] >= 0.55  # floor
    # Size multiplier shows the shrink.
    assert decision["size_multiplier"] < 1.00


# ── Runtime mode round-trip ──────────────────────────────────────

@pytest.mark.asyncio
async def test_runtime_mode_defaults_to_disarmed():
    # Wipe any existing arbiter subdoc first to force the default.
    await db[BRM].update_one(
        {"_id": STACK_ID},
        {"$unset": {"arbiter": ""}},
    )
    mode = await get_runtime_mode()
    assert mode == RuntimeMode.DISARMED


@pytest.mark.asyncio
async def test_runtime_mode_flip_records_actor():
    receipt = await set_runtime_mode(RuntimeMode.LIVE, actor="test@risedual.io")
    assert receipt["ok"] is True
    assert receipt["runtime_mode"] == "LIVE"
    assert receipt["actor"] == "test@risedual.io"
    # And the read reflects the change.
    mode = await get_runtime_mode()
    assert mode == RuntimeMode.LIVE
    # Reset to DISARMED for the next test.
    await set_runtime_mode(RuntimeMode.DISARMED, actor="test@risedual.io")
