"""MC Arbiter — background grader.

Grades opinions against real market movement at 15m and 60m
horizons, folds each grade into the (brain, lane) DAWE state via
EWMA.

Design freeze: `/app/memory/MC_SEAT_ARBITER.md` §6.

This is NOT shadow trading. No simulated fills, no paper P&L.
Every grade is a real prediction vs a real subsequent price. That
distinction matters — see design freeze §7 / operator directive
"no more shadow anything applied to the active pipeline".
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from db import db
from mc_arbiter.arbiter import BRM, MC_SEATS, STACK_ID, load_dawe, save_dawe
from mc_arbiter.dawe import (
    quality_from_signed_return,
    update_recent,
    update_session,
)
from mc_arbiter.models import Direction

logger = logging.getLogger("mc_arbiter.grader")

# ── Grade horizons (design freeze §6) ────────────────────────────
HORIZON_15M = timedelta(minutes=15)
HORIZON_60M = timedelta(minutes=60)

# ── Expected-move fallback ───────────────────────────────────────
# When ATR isn't available on the bar we use a conservative default
# — 0.5% for equity, 1.0% for crypto. See design freeze §6:
# `expected_move` scales the signed_return into the [0,1] quality
# band, so being too small over-rewards small moves and being too
# large under-rewards big ones. These defaults are round enough to
# be honest until we wire a real ATR lookup in Phase 2.
FALLBACK_EXPECTED_MOVE_FRAC = {"equity": 0.005, "crypto": 0.010}


# ── One pass ─────────────────────────────────────────────────────

async def grade_pending_opinions(now: datetime | None = None) -> dict:
    """One grading pass. Idempotent — an opinion is only graded
    once per horizon (`grade_15m` / `grade_60m` fields on the
    opinion doc gate re-grades).

    Returns a summary: number of opinions graded at each horizon,
    number of DAWE updates written.
    """
    now = now or datetime.now(timezone.utc)
    graded_15 = await _grade_horizon(now, HORIZON_15M, "grade_15m")
    graded_60 = await _grade_horizon(now, HORIZON_60M, "grade_60m")

    # Fold each new 15m grade into session_weight. 60m grades feed
    # session too (double-counting is acceptable at v0.1 — the EWMA
    # α=0.30 already smooths this; a 60m confirm just reinforces
    # what the 15m grade already said).
    total_dawe_updates = 0
    for graded in graded_15 + graded_60:
        state = await load_dawe(graded["brain"], graded["lane"])
        state.session_weight = update_session(
            prev_weight=state.session_weight,
            observed_quality=graded["quality"],
        )
        state.grades_used_session += 1
        # `recent` moves via the daily rollup (see roll_recent_end_of_day)
        await save_dawe(state)
        total_dawe_updates += 1

    return {
        "ok": True,
        "graded_15m": len(graded_15),
        "graded_60m": len(graded_60),
        "dawe_updates": total_dawe_updates,
        "ran_at": now.isoformat(),
    }


async def _grade_horizon(
    now: datetime, horizon: timedelta, grade_field: str,
) -> list[dict]:
    """Grade every opinion aged ≥ `horizon` without a `grade_field`
    receipt. Returns a compact list of what was graded (for the
    DAWE fold above)."""
    cutoff = (now - horizon).isoformat()
    # Find opinions old enough + not yet graded at this horizon.
    cursor = (
        db[MC_SEATS]
        .find(
            {"ts": {"$lte": cutoff}, grade_field: None},
            {"_id": 0},
        )
        .max_time_ms(2500)
        .limit(200)
    )
    graded_out: list[dict] = []
    async for op in cursor:
        try:
            grade = await _grade_one(op, now, horizon)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "grade failure seat=%s brain=%s err=%s",
                op.get("seat_key"), op.get("brain"), exc,
            )
            continue
        if grade is None:
            continue
        await db[MC_SEATS].update_one(
            {"seat_key": op["seat_key"], "brain": op["brain"]},
            {"$set": {grade_field: grade}},
        )
        graded_out.append({
            "brain": op["brain"],
            "lane": op["lane"],
            "seat_key": op["seat_key"],
            "quality": grade["quality"],
            "horizon": grade["horizon"],
        })
    return graded_out


async def _grade_one(
    op: dict, now: datetime, horizon: timedelta,
) -> dict | None:
    """Compute one grade. Returns None when we cannot fetch a price
    at the horizon (missing bar, symbol not covered) — the opinion
    stays ungraded and will be retried on the next grader tick."""
    symbol = op["symbol"]
    lane = op["lane"]
    direction = op["direction"]
    price_at_signal = float(op["price_at_signal"])
    if price_at_signal <= 0:
        return None

    horizon_ts = datetime.fromisoformat(op["ts"]).astimezone(timezone.utc) + horizon
    price_at_horizon = await _price_at(symbol, horizon_ts)
    if price_at_horizon is None or price_at_horizon <= 0:
        return None

    # signed_return: + for correct LONG, + for correct SHORT
    # (return * -1), zero for FLAT (grading is symmetric).
    raw_return = price_at_horizon / price_at_signal - 1.0
    if direction == Direction.LONG.value:
        signed = raw_return
    elif direction == Direction.SHORT.value:
        signed = -raw_return
    elif direction == Direction.FLAT.value:
        # FLAT: reward for avoiding harm on a chop day, penalize
        # for avoiding upside on a strong day. Use the absolute
        # move magnitude — small |return| = correct FLAT, big
        # |return| = missed opportunity.
        signed = -abs(raw_return)
    else:
        return None

    expected = FALLBACK_EXPECTED_MOVE_FRAC.get(lane, 0.005)
    quality = quality_from_signed_return(
        signed_return=signed, expected_move=expected,
    )
    return {
        "horizon": int(horizon.total_seconds() // 60),  # minutes
        "graded_at": now.isoformat(),
        "price_at_horizon": price_at_horizon,
        "signed_return": signed,
        "expected_move": expected,
        "quality": quality,
    }


async def _price_at(symbol: str, when: datetime) -> float | None:
    """Latest 1m close at or before `when`. Bounded read.

    We take "at or before" rather than "closest to" so the grade
    is deterministic and never uses information from AFTER the
    horizon (that would be lookahead bias — the exact class of
    dishonesty the anti-shadow doctrine targets)."""
    when_iso = when.isoformat()
    doc = await (
        db["shared_ohlcv_bars"]
        .find_one(
            {"symbol": symbol, "tf": "1m", "ts": {"$lte": when_iso}},
            sort=[("ts", -1)],
            max_time_ms=1500,
        )
    )
    if not doc:
        return None
    close = doc.get("close") or doc.get("c")
    return float(close) if close else None


# ── Daily rollup (end-of-session) ────────────────────────────────

async def roll_recent_end_of_day() -> dict:
    """Once per day, fold each (brain, lane)'s current
    session_weight into `recent_weight` and reset session grade
    counts. Called from the daily worker cron (Phase 2 wiring).

    Design freeze §4: `recent` moves at α=0.10 → slow enough that
    a bad day doesn't erase weeks of real signal.
    """
    doc = await db[BRM].find_one(
        {"_id": STACK_ID}, {"brains": 1},
    )
    brains = (doc or {}).get("brains") or {}
    rolled = 0
    for brain, section in brains.items():
        dawe_map = ((section or {}).get("dawe") or {})
        for lane in dawe_map.keys():
            state = await load_dawe(brain, lane)
            state.recent_weight = update_recent(
                prev_weight=state.recent_weight,
                session_average=state.session_weight,
            )
            state.grades_used_recent += state.grades_used_session
            state.grades_used_session = 0
            # Reset session_weight toward 1.0 slightly — new day,
            # partial memory reset. We DON'T reset to 1.0 exactly
            # (that'd erase intraday signal), but move 30% back
            # toward neutral. This is the operator's "yesterday
            # matters less" doctrine encoded numerically.
            state.session_weight = 0.7 * state.session_weight + 0.3 * 1.0
            await save_dawe(state)
            rolled += 1
    return {"ok": True, "rolled": rolled, "ts": _now_iso()}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
