"""Opinion Market Resolver Worker — auto-grades directional opinions.

Doctrine (2026-05-24, operator priority):

    458/485 outcomes in `shared_brain_outcomes` are operator-driven; only
    27 from Chevelle. Alpha/Camaro/REDEYE never auto-resolve. The
    operator is clicking through every win/loss verdict by hand.

    This worker closes the gap for DIRECTIONAL stances only. After a
    `long` or `short` opinion sits N hours past its post timestamp,
    the worker reads market price, computes the trade-direction PnL,
    and writes an outcome with `resolved_by="auto:market-data"`.

    What this worker DOES NOT touch:
      * `observation` stances — informational, not directional
      * `endorse` / `veto` stances — interpretive judgments that need
        a downstream trade outcome, not just price
      * Already-resolved opinions (idempotency by opinion_id uniqueness)

    Implementation mirrors `observation_resolver.py`:
      * Uses the same price-fetch surface (`_fetch_price`)
      * Lane-aware thresholds (crypto ±2%, equity ±1%) — matches
        `OUTCOME_THRESHOLDS` in observation_resolver to keep the two
        grading systems on the same scale
      * Idempotent — re-running never double-writes an outcome
        (existing `(opinion_id) unique` in `shared_brain_outcomes`)

Auth: this is a background worker, no auth surface.
"""
from __future__ import annotations

import asyncio
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from db import db
from namespaces import SHARED_OPINIONS, SHARED_OUTCOMES


logger = logging.getLogger(__name__)


# How often to scan for resolvable opinions.
RESOLVER_TICK_SECONDS = int(
    os.environ.get("OPINION_RESOLVER_TICK_SEC", "300"),
)
# Horizon — opinions older than this get auto-resolved. Default 24h
# is aggressive on purpose (operator wants learning loops to close);
# operator can dial up via env if 24h is too noisy.
RESOLUTION_HORIZON_HOURS = float(
    os.environ.get("OPINION_RESOLUTION_HORIZON_HOURS", "24"),
)
# Outcome thresholds — same as observation_resolver for consistency.
OUTCOME_THRESHOLDS = {
    "crypto": 0.0200,   # ±2.0%
    "equity": 0.0100,   # ±1.0%
}
# Stances eligible for auto-resolution. Anything else stays
# operator-driven (the doctrine pin above).
DIRECTIONAL_STANCES = {"long", "short"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(iso: str) -> Optional[datetime]:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _classify_outcome(pnl_pct: float, lane: str) -> str:
    """Three-way classification — same shape that `_hit_rate` consumes."""
    bar = OUTCOME_THRESHOLDS.get(lane, 0.01)
    if pnl_pct > bar:
        return "win"
    if pnl_pct < -bar:
        return "loss"
    return "no-event"


def _sided_pnl_pct(stance: str, anchor: float, current: float) -> float:
    """`long` is graded as price ↑ = win. `short` is graded as price ↓
    = win."""
    if anchor <= 0:
        return 0.0
    raw = (current - anchor) / anchor
    return -raw if stance.lower() == "short" else raw


def _lane_for_topic(topic: str) -> str:
    """Topic field convention: `symbol:BTCUSD` or `symbol:AAPL`. Lane
    is inferred from symbol shape — crypto symbols include common
    quote currencies; everything else is equity. This is the same
    heuristic the rest of MC uses (`shared/intents.py::_compose_canonical`).
    """
    if not topic or ":" not in topic:
        return "equity"
    symbol = topic.split(":", 1)[1].upper()
    for suffix in ("USD", "USDT", "USDC", "BTC", "ETH"):
        if symbol.endswith(suffix) and len(symbol) > len(suffix):
            return "crypto"
    # Bare BTC / ETH / etc. on Kraken are crypto.
    if symbol in {"BTC", "ETH", "SOL", "ADA", "DOGE", "XRP", "DOT"}:
        return "crypto"
    return "equity"


def _symbol_from_topic(topic: str) -> Optional[str]:
    if not topic or ":" not in topic:
        return None
    return topic.split(":", 1)[1].upper()


# ─────────────────────────── price fetch ───────────────────────────


async def _fetch_anchor_price(opinion: dict) -> Optional[float]:
    """The anchor is the price at the time the opinion was posted.
    Opinions don't currently carry an anchor field, so we synthesise
    from the symbol's market price at the (posted_at) timestamp where
    possible. For the v1 worker we fall back to the current price
    minus an estimated drift — acceptable because the 24h horizon is
    short enough that anchor noise is bounded for grading purposes.

    Future improvement: persist anchor_price on opinion at post time
    (matches the observation_receipt pattern). Tracked in CHANGELOG.
    """
    # If anchor is already on the opinion, use it.
    anchor = opinion.get("anchor_price")
    if anchor is not None and anchor > 0:
        return float(anchor)
    # No anchor on the opinion — return None. The grader will skip and
    # the opinion stays unresolved until a future operator backfill or
    # until the brain teams add anchor_price to opinion posts.
    return None


async def _fetch_current_price(symbol: str, lane: str) -> Optional[float]:
    """Reuses the observation resolver's price fetcher to keep one
    source of truth for market data."""
    from shared.observation_resolver import _fetch_price  # noqa: WPS433
    return await _fetch_price(symbol, lane)


# ─────────────────────────── grading ───────────────────────────


async def _grade_opinion(opinion: dict) -> Optional[dict]:
    """Compute an outcome doc for one directional opinion, or None if
    not yet gradeable."""
    stance = (opinion.get("stance") or "").lower()
    if stance not in DIRECTIONAL_STANCES:
        return None
    posted = _parse_iso(opinion.get("posted_at"))
    if posted is None:
        return None
    age_h = (_now() - posted).total_seconds() / 3600.0
    if age_h < RESOLUTION_HORIZON_HOURS:
        return None  # not old enough

    topic = opinion.get("topic") or ""
    symbol = _symbol_from_topic(topic)
    if not symbol:
        return None
    lane = _lane_for_topic(topic)

    anchor = await _fetch_anchor_price(opinion)
    if anchor is None or anchor <= 0:
        # Can't grade without anchor — log once for visibility but
        # don't poison-pill the opinion (let an operator resolve it
        # by hand or wait for anchor_price to be added at post time).
        return None
    current = await _fetch_current_price(symbol, lane)
    if current is None or current <= 0:
        return None  # try next tick

    pnl_pct = _sided_pnl_pct(stance, anchor, current)
    label = _classify_outcome(pnl_pct, lane)

    return {
        "outcome_id": str(uuid.uuid4()),
        "opinion_id": opinion["opinion_id"],
        "runtime": opinion["runtime"],
        "topic": topic,
        "stance": opinion["stance"],
        "confidence": float(opinion.get("confidence", 0.5)),
        "regime": opinion.get("regime"),
        "posted_at": opinion["posted_at"],
        "resolved_at": _now().isoformat(),
        "resolved_by": "auto:market-data",
        "actual": label,
        "notes": (
            f"auto-graded: anchor={anchor:.6f} current={current:.6f} "
            f"pnl_pct={pnl_pct:+.4f} threshold={OUTCOME_THRESHOLDS.get(lane, 0.01)} "
            f"lane={lane}"
        ),
        "anchor_price": anchor,
        "current_price": current,
        "pnl_pct": round(pnl_pct, 6),
        "horizon_hours": round(age_h, 2),
    }


async def _tick() -> dict:
    """One pass through unresolved directional opinions. Returns a
    summary count dict."""
    cutoff = _now() - timedelta(hours=RESOLUTION_HORIZON_HOURS)
    cutoff_iso = cutoff.isoformat()

    # All directional opinions older than the horizon. We re-check
    # the "no existing outcome" condition per-opinion below because a
    # bulk join here would be expensive.
    candidates_cursor = db[SHARED_OPINIONS].find(
        {
            "stance": {"$in": list(DIRECTIONAL_STANCES)},
            "posted_at": {"$lte": cutoff_iso},
        },
        {"_id": 0},
    ).limit(500)  # cap per tick

    scanned = 0
    graded = 0
    skipped_no_anchor = 0
    skipped_already = 0
    skipped_no_price = 0

    async for opinion in candidates_cursor:
        scanned += 1
        # Idempotency — skip if already resolved.
        existing = await db[SHARED_OUTCOMES].find_one(
            {"opinion_id": opinion["opinion_id"]},
            {"_id": 1},
        )
        if existing:
            skipped_already += 1
            continue

        outcome = await _grade_opinion(opinion)
        if outcome is None:
            # Distinguish "no anchor" from "no price" for telemetry.
            if not opinion.get("anchor_price"):
                skipped_no_anchor += 1
            else:
                skipped_no_price += 1
            continue

        # Write outcome — race-safe via opinion_id uniqueness check we
        # already did. If two workers raced and both got here, the
        # `try` catches the duplicate.
        try:
            await db[SHARED_OUTCOMES].insert_one(outcome)
            graded += 1
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "opinion_resolver: insert failed opinion_id=%s err=%r",
                opinion.get("opinion_id"), e,
            )

    return {
        "scanned": scanned,
        "graded": graded,
        "skipped_already_resolved": skipped_already,
        "skipped_no_anchor": skipped_no_anchor,
        "skipped_no_price": skipped_no_price,
        "horizon_hours": RESOLUTION_HORIZON_HOURS,
    }


# ─────────────────────────── runner ───────────────────────────


_worker_task: Optional[asyncio.Task] = None


async def _loop():
    """Main background loop. Idempotent if called more than once
    (the second call's task gets immediately cancelled by the
    `start_worker` guard)."""
    logger.info(
        "opinion_resolver: started tick=%ss horizon=%sh",
        RESOLVER_TICK_SECONDS, RESOLUTION_HORIZON_HOURS,
    )
    while True:
        try:
            stats = await _tick()
            if stats.get("graded", 0) > 0:
                logger.info("opinion_resolver tick: %s", stats)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning("opinion_resolver: tick error %r", e)
        await asyncio.sleep(RESOLVER_TICK_SECONDS)


def start_worker() -> None:
    """Start the background task. No-op if already running."""
    global _worker_task
    if _worker_task is not None and not _worker_task.done():
        return
    _worker_task = asyncio.create_task(_loop())


def stop_worker() -> None:
    """Cancel the background task (used in tests + graceful shutdown)."""
    global _worker_task
    if _worker_task is not None and not _worker_task.done():
        _worker_task.cancel()
    _worker_task = None
