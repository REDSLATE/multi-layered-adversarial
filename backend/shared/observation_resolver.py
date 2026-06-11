"""Observation Resolver Worker — grades observation receipts.

Doctrine pin (2026-02-18, Phase 2 of ladder doctrine):

Phase 1 created observation receipts. They sat at `resolved=False`
with anchor prices but no outcome. This worker fills in the grading.

Loop:
    Every RESOLVER_TICK_SECONDS (default 300s = 5min):
      for each unresolved observation receipt:
        * Fetch current market price (Alpaca for equity, Kraken for crypto)
        * Update running MFE / MAE on the receipt (max favorable /
          adverse excursion since anchor, signed by side)
        * For each horizon (+1h, +4h, +1d, +5d):
            - If horizon elapsed AND not yet recorded → record price
        * If 5d horizon recorded → flip resolved=True, compute outcome

Outcome classification (per lane):
    crypto: |pnl_pct| < 0.20 → neutral; > +0.20 win (sided); < -0.20 loss
    equity: |pnl_pct| < 0.10 → neutral; > +0.10 win (sided); < -0.10 loss

Per-side sign:
    BUY/COVER  → pnl_pct = (current - anchor) / anchor
    SELL/SHORT → pnl_pct = (anchor - current) / anchor

Failure modes:
    * Price fetch failure → silent; receipt stays unresolved; retry next tick
    * Missing anchor_price → receipt is unresolvable; mark
      `resolved=True outcome="anchor_missing"` so it stops being retried

The worker is read-only on the broker side (no orders, no balances).
Only uses public ticker endpoints + already-cached equity prices.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Optional

from db import db
from namespaces import OBSERVATION_RECEIPTS


logger = logging.getLogger(__name__)


RESOLVER_TICK_SECONDS = int(os.environ.get("OBSERVATION_RESOLVER_TICK_SEC", "300"))

# Horizon offsets in seconds from receipt.created_at.
HORIZONS = {
    "1h":  60 * 60,
    "4h":  4 * 60 * 60,
    "1d":  24 * 60 * 60,
    "5d":  5 * 24 * 60 * 60,
}

# Outcome thresholds — tuned conservatively. Crypto is more volatile, so
# its bar for "win" is larger to filter noise.
OUTCOME_THRESHOLDS = {
    "crypto": 0.0200,   # ±2.0%
    "equity": 0.0100,   # ±1.0%
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(iso: str) -> Optional[datetime]:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _sided_pnl_pct(side: str, anchor: float, current: float) -> float:
    """Compute pnl_pct from the trade's directional intent."""
    if anchor <= 0:
        return 0.0
    raw = (current - anchor) / anchor
    side_u = (side or "").upper()
    return -raw if side_u in {"SELL", "SHORT"} else raw


def _classify_outcome(pnl_pct: float, lane: str) -> str:
    """Win / loss / neutral based on lane-specific thresholds."""
    bar = OUTCOME_THRESHOLDS.get(lane, 0.01)
    if pnl_pct > bar:
        return "win"
    if pnl_pct < -bar:
        return "loss"
    return "neutral"


# ─────────────────────────── price fetch ───────────────────────────


async def _fetch_price(symbol: str, lane: str) -> Optional[float]:
    """Get current market price for one symbol. Lane-aware: routes
    to Alpaca for equity, Kraken public ticker for crypto. Returns
    None on any failure."""
    if lane == "crypto":
        try:
            from shared.risk.position_monitor import _crypto_price_for  # noqa: WPS433
            return await _crypto_price_for(symbol)
        except Exception as e:  # noqa: BLE001
            logger.warning("resolver: crypto price fetch failed sym=%s err=%r", symbol, e)
            return None
    # equity
    try:
        from shared.broker_router import adapter_for_lane  # noqa: WPS433
        adapter = await adapter_for_lane("equity")
        if adapter is None:
            return None
        # Some adapters expose `get_latest_trade`; fall back to
        # list_positions current_price when the surface isn't available.
        try:
            trade = await adapter.get_latest_trade(symbol)
            if trade and trade.get("price"):
                return float(trade["price"])
        except AttributeError:
            pass
        positions = await adapter.list_positions()
        for p in positions or []:
            if (p.get("symbol") or "").upper() == symbol.upper():
                px = p.get("current_price")
                if px is not None and px > 0:
                    return float(px)
        return None
    except Exception as e:  # noqa: BLE001
        logger.warning("resolver: equity price fetch failed sym=%s err=%r", symbol, e)
        return None


# ─────────────────────────── grading ───────────────────────────


async def _grade_receipt(receipt: dict) -> Optional[dict]:
    """Compute the update dict for one observation receipt's row.
    Returns the `$set` payload (or None if no update is warranted)."""
    anchor = receipt.get("anchor_price")
    if anchor is None or anchor <= 0:
        # Unresolvable — mark resolved=True with diagnostic outcome so
        # we stop retrying.
        return {
            "resolved": True,
            "resolved_at": _now().isoformat(),
            "outcome": "anchor_missing",
            "pnl_pct": None,
        }
    created = _parse_iso(receipt.get("created_at"))
    if created is None:
        return None

    symbol = receipt.get("symbol") or ""
    lane = (receipt.get("lane") or "").lower()
    side = (receipt.get("side") or "").upper()
    current = await _fetch_price(symbol, lane)
    if current is None or current <= 0:
        return None  # try again next tick

    age = (_now() - created).total_seconds()

    # Build the update doc incrementally.
    update: dict = {}

    # Always update running MFE / MAE.
    pnl_now = _sided_pnl_pct(side, anchor, current)
    prior_mfe = receipt.get("mfe_pct")
    prior_mae = receipt.get("mae_pct")
    if prior_mfe is None or pnl_now > prior_mfe:
        update["mfe_pct"] = round(pnl_now, 6)
    if prior_mae is None or pnl_now < prior_mae:
        update["mae_pct"] = round(pnl_now, 6)

    # Record any horizons that have elapsed but not yet been captured.
    horizon_prices = dict(receipt.get("horizon_prices") or {})
    changed_horizon = False
    for label, seconds in HORIZONS.items():
        if age >= seconds and label not in horizon_prices:
            horizon_prices[label] = round(current, 8)
            changed_horizon = True
    if changed_horizon:
        update["horizon_prices"] = horizon_prices

    # Resolution: if 5d horizon reached, finalize.
    if "5d" in horizon_prices:
        final_pnl = _sided_pnl_pct(side, anchor, horizon_prices["5d"])
        update["resolved"] = True
        update["resolved_at"] = _now().isoformat()
        update["pnl_pct"] = round(final_pnl, 6)
        update["outcome"] = _classify_outcome(final_pnl, lane)

    return update or None


# ─────────────────────────── loop ───────────────────────────


async def _resolver_tick() -> int:
    """One pass. Returns the number of receipts touched."""
    cursor = db[OBSERVATION_RECEIPTS].find(
        {"resolved": False},
        {"_id": 0},
    ).limit(500)
    touched = 0
    async for receipt in cursor:
        try:
            update = await _grade_receipt(receipt)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "resolver: grade failed intent_id=%s err=%r",
                receipt.get("intent_id"), e,
            )
            continue
        if not update:
            continue
        await db[OBSERVATION_RECEIPTS].update_one(
            {"intent_id": receipt.get("intent_id")},
            {"$set": update},
        )
        touched += 1
    if touched:
        logger.info("resolver: graded %d observation receipts", touched)
    return touched


_TASK: Optional[asyncio.Task] = None


async def _resolver_loop() -> None:
    """Main async loop. Sleeps between ticks; survives errors."""
    logger.info(
        "observation resolver: started tick=%ss horizons=%s",
        RESOLVER_TICK_SECONDS, list(HORIZONS),
    )
    while True:
        try:
            await _resolver_tick()
        except Exception as e:  # noqa: BLE001
            logger.warning("resolver: tick failed err=%r", e)
        try:
            await asyncio.sleep(RESOLVER_TICK_SECONDS)
        except asyncio.CancelledError:
            break


async def start_observation_resolver() -> None:
    """Lifespan entry point. Idempotent."""
    global _TASK
    if _TASK is not None and not _TASK.done():
        return
    _TASK = asyncio.create_task(_resolver_loop())


async def stop_observation_resolver() -> None:
    global _TASK
    if _TASK is None:
        return
    _TASK.cancel()
    try:
        await _TASK
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass
    _TASK = None
