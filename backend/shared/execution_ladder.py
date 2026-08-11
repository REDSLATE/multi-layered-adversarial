"""Execution Recovery Ladder (2026 MC directive — "Capture the Move,
Don't Return to HOLD").

A wide spread is execution FRICTION, not a rejection. Instead of
dropping a qualified signal back to HOLD, the router hunts a fill
down a price-disciplined ladder:

  1. maker_bid         post-only limit at best bid
  2. maker_reprice     cancel + post-only at the fresh bid
  3. adaptive_maker    post-only at bid + 25% of the spread
  4. aggressive_limit  marketable limit capped at min(ask, chase cap)
  5. abandon           record qualified_but_unexecuted (LadderUnfilled)

NEVER a market order. Every terminal state (fill or abandon) lands in
`execution_ladder_events` so the ladder can never become a hidden
rejection gate. Knobs: runtime_flags `_id=execution_ladder`.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger("risedual.execution_ladder")

FLAG_ID = "execution_ladder"
EVENTS = "execution_ladder_events"
ACTIVE = "execution_ladder_active"
DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "stage_wait_s": 7.0,
    "poll_s": 2.0,
    "adaptive_spread_frac": 0.25,
    "max_chase_bps": 100.0,
}
_STAGES = ("maker_bid", "maker_reprice", "adaptive_maker", "aggressive_limit")


class LadderUnfilled(Exception):
    """Ladder exhausted without a fill — qualified_but_unexecuted."""


async def get_ladder_config() -> dict:
    try:
        from db import db  # noqa: WPS433
        doc = await db["runtime_flags"].find_one(
            {"_id": FLAG_ID}, {"_id": 0}, max_time_ms=3000) or {}
    except Exception:  # noqa: BLE001
        doc = {}
    return {**DEFAULTS, **doc}


async def ladder_enabled() -> bool:
    return bool((await get_ladder_config()).get("enabled", True))


def _signal_price(intent: dict) -> Optional[float]:
    for key in ("price_at_signal", "price", "entry_price"):
        v = intent.get(key) or (intent.get("evidence") or {}).get(key)
        try:
            if v and float(v) > 0:
                return float(v)
        except (TypeError, ValueError):
            continue
    return None


async def _record_event(intent: dict, *, outcome: str, stage: str,
                        stages: list[str], notional_usd: float,
                        detail: str = "") -> None:
    try:
        from db import db  # noqa: WPS433
        await db[EVENTS].insert_one({
            "ts": datetime.now(timezone.utc).isoformat(),
            "intent_id": intent.get("intent_id"),
            "symbol": intent.get("symbol"),
            "lane": (intent.get("lane") or "crypto").lower(),
            "outcome": outcome,
            "final_stage": stage,
            "stages_attempted": stages,
            "notional_usd": round(float(notional_usd), 2),
            "spread_bps": (intent.get("risk_sizing") or {}).get("signal_spread_bps"),
            "detail": (detail or "")[:300],
        })
    except Exception as exc:  # noqa: BLE001
        logger.warning("ladder event record failed: %s", exc)


async def _set_active(intent: dict, *, stage: str, limit_price: float,
                      notional_usd: float) -> None:
    """Live hunt heartbeat — the UI polls this for real-time toasts."""
    try:
        from db import db  # noqa: WPS433
        now = datetime.now(timezone.utc).isoformat()
        await db[ACTIVE].update_one(
            {"intent_id": intent.get("intent_id")},
            {"$set": {
                "symbol": intent.get("symbol"),
                "lane": (intent.get("lane") or "crypto").lower(),
                "stage": stage,
                "limit_price": limit_price,
                "notional_usd": round(float(notional_usd), 2),
                "updated_at": now,
            }, "$setOnInsert": {"started_at": now}},
            upsert=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("ladder active upsert failed: %s", exc)


async def _clear_active(intent: dict) -> None:
    try:
        from db import db  # noqa: WPS433
        await db[ACTIVE].delete_one({"intent_id": intent.get("intent_id")})
    except Exception as exc:  # noqa: BLE001
        logger.warning("ladder active clear failed: %s", exc)


def _fill_qty(info: Optional[dict]) -> float:
    """Executed base volume from a normalized get_order response —
    Kraken's raw vol_exec carries partial fills even on canceled orders."""
    if not info:
        return 0.0
    try:
        q = info.get("filled_qty")
        if q:
            return float(q)
        return float((info.get("raw") or {}).get("vol_exec") or 0)
    except (TypeError, ValueError):
        return 0.0


async def _poll_fill(adapter, order_id: str, *, wait_s: float,
                     poll_s: float) -> tuple[str, Optional[dict]]:
    """Poll until stage deadline. Returns (state, info):
    filled | gone (canceled/expired) | open (deadline hit)."""
    deadline = time.monotonic() + wait_s
    info = None
    while time.monotonic() < deadline:
        await asyncio.sleep(min(poll_s, max(deadline - time.monotonic(), 0.1)))
        try:
            info = await adapter.get_order(order_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("ladder poll failed %s: %s", order_id, exc)
            continue
        st = (info.get("status") or "").upper()
        if st == "FILLED":
            return "filled", info
        if st in ("CANCELED", "CANCELLED", "EXPIRED", "REJECTED", "FAILED"):
            return "gone", info
    return "open", info


async def _cancel(adapter, order_id: str) -> Optional[dict]:
    try:
        await adapter.cancel_order(order_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("ladder cancel failed %s: %s", order_id, exc)
    try:
        return await adapter.get_order(order_id)
    except Exception:  # noqa: BLE001
        return None


async def run_entry_ladder(
    adapter,
    *,
    intent: dict,
    broker_symbol: str,
    notional_usd: float,
    client_order_id: Optional[str] = None,
    mc_receipt: Optional[dict] = None,
) -> dict:
    """Hunt a BUY fill down the ladder. Returns the (possibly partial)
    filled order dict, or raises LadderUnfilled after recording the
    qualified_but_unexecuted event. Total budget ≈ 4 × stage_wait_s."""
    cfg = await get_ladder_config()
    wait_s = float(cfg["stage_wait_s"])
    poll_s = float(cfg["poll_s"])
    sig_px = _signal_price(intent)
    chase_cap = (sig_px * (1.0 + float(cfg["max_chase_bps"]) / 10_000.0)
                 if sig_px else None)
    from shared.crypto.broker_adapter import _ticker_bid_ask  # noqa: WPS433
    from shared.crypto.kraken import to_kraken_pair  # noqa: WPS433
    pair = to_kraken_pair(broker_symbol) if "/" in broker_symbol else broker_symbol

    attempted: list[str] = []
    last_detail = ""
    for stage in _STAGES:
        try:
            bid, ask = await _ticker_bid_ask(pair)
        except Exception as exc:  # noqa: BLE001
            last_detail = f"{stage}_quote_unavailable: {str(exc)[:120]}"
            continue
        spread = max(ask - bid, 0.0)
        post_only = stage != "aggressive_limit"
        if stage == "adaptive_maker":
            px = min(bid + spread * float(cfg["adaptive_spread_frac"]),
                     ask * 0.9999)
            px = max(px, bid)
        elif stage == "aggressive_limit":
            px = ask if chase_cap is None else min(ask, chase_cap)
            if px < bid:
                # cap sits BELOW the book — the move ran past discipline
                attempted.append(stage)
                last_detail = (f"price_ran_away: ask={ask} bid={bid} "
                               f"chase_cap={chase_cap}")
                break
        else:
            px = bid
        attempted.append(stage)
        qty = float(notional_usd) / px
        await _set_active(intent, stage=stage, limit_price=px,
                          notional_usd=notional_usd)
        try:
            order = await adapter.submit_limit_order(
                symbol=broker_symbol,
                qty=qty,
                limit_price=px,
                side="BUY",
                client_order_id=client_order_id,
                mc_receipt=mc_receipt,
                post_only=post_only,
                expire_s=int(wait_s) + 5,
            )
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            last_detail = f"{stage}_submit_failed: {msg[:160]}"
            if "post only" not in msg.lower():
                logger.warning("ladder submit failed %s %s: %s",
                               stage, pair, msg)
            continue  # post-only would cross / transient — climb a rung
        order_id = order.get("order_id")
        state, info = await _poll_fill(adapter, order_id,
                                       wait_s=wait_s, poll_s=poll_s)
        if state == "open":
            info = await _cancel(adapter, order_id) or info
            if ((info or {}).get("status") or "").upper() == "FILLED":
                state = "filled"
        filled_qty = _fill_qty(info)
        if state == "filled" or filled_qty > 0:
            order.update({
                "status": "filled" if state == "filled" else "partially_filled",
                "filled_qty": filled_qty or order.get("volume_base"),
                "filled_avg_price": (info or {}).get("filled_avg_price") or px,
                "filled_at": (info or {}).get("filled_at"),
                "order_style": "recovery_ladder",
                "ladder_stage": stage,
                "ladder_stages_attempted": attempted,
            })
            await _record_event(intent, outcome="filled", stage=stage,
                                stages=attempted, notional_usd=notional_usd,
                                detail=f"limit={px} state={state}")
            await _clear_active(intent)
            logger.info("ladder FILLED %s at stage=%s px=%s (%s)",
                        pair, stage, px, state)
            return order
        last_detail = f"{stage}_unfilled at limit={px}"

    final_stage = attempted[-1] if attempted else "none"
    await _record_event(intent, outcome="qualified_but_unexecuted",
                        stage=final_stage, stages=attempted,
                        notional_usd=notional_usd, detail=last_detail)
    await _clear_active(intent)
    logger.info("ladder ABANDONED %s after %s: %s", pair, attempted, last_detail)
    raise LadderUnfilled(
        f"ladder exhausted after {'/'.join(attempted) or 'no stages'}: "
        f"{last_detail}")
